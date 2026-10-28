//! Explicit v2 blackbox case. V1 remains in its byte-identical original module.
#[path = "system_case_blackbox_contract.rs"]
mod contract;
use crate::optimize::sysparam;
use anyhow::{bail, ensure, Context, Result};
use clap::Subcommand;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::ffi::CString;
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const JSON_CAP: usize = 2 * 1024 * 1024;
const TOOL_CAP: usize = 128 * 1024 * 1024;
const EXEC_CAP: u64 = 512 * 1024 * 1024;
const PHASES: [&str; 4] = ["prepare", "freeze", "collect", "judge"];

#[derive(Debug, Subcommand)]
pub enum Action {
    /// Offline deterministic candidate, request, workload and budget snapshot.
    Plan {
        #[arg(long)]
        profile: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
    /// Single-use receiver prepare -> freeze -> collect -> judge. Never resumes.
    Run {
        #[arg(long)]
        case: PathBuf,
        #[arg(long)]
        phonefarm_bin: PathBuf,
        #[arg(long)]
        execute: bool,
    },
    /// Read current integrity and exact recorded state; never execute or recover.
    Inspect {
        #[arg(long)]
        case: PathBuf,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Target {
    kind: String,
    program: PathBuf,
    target: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Carrier {
    kind: String,
    application_version: String,
    scene_id: String,
    reset_semantics: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Budgets {
    prepare_seconds: u64,
    collect_seconds: u64,
    local_seconds: u64,
    whole_seconds: u64,
    stream_bytes: usize,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Profile {
    schema: String,
    case_id: String,
    transport: Target,
    package: String,
    render_comm: String,
    carrier: Carrier,
    probe: PathBuf,
    workload: PathBuf,
    control_id: String,
    candidate_value: String,
    capture_seconds: u64,
    lead_seconds: u64,
    pairs: usize,
    paired_t_assumptions_acknowledged: bool,
    temperature_profile_id: Option<String>,
    goal: Option<String>,
    render_attribution_profile_id: String,
    render_reader_artifact: PathBuf,
    source_budget_calls: u32,
    budgets: Budgets,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Identity {
    sha256: String,
    bytes: u64,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema: String,
    files: BTreeMap<String, Identity>,
    implementation_sha256: String,
    candidate_generator_sha256: String,
    carrier_context: String,
    real_game_optimization_proven: bool,
    promotion_allowed: bool,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Workload {
    schema: String,
    actions: Vec<Touch>,
}
#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum Touch {
    Tap {
        x: u32,
        y: u32,
    },
    Swipe {
        x1: u32,
        y1: u32,
        x2: u32,
        y2: u32,
        duration_ms: u32,
    },
    Sleep {
        seconds: u64,
    },
}

// All file operations are anchored to opened directory FDs. Symlink ancestors,
// FIFO/device files, traversal and unbounded files are refused before reading.
struct Dir(File);
fn name(s: &str) -> Result<CString> {
    ensure!(
        !s.is_empty() && s != "." && s != ".." && !s.contains('/'),
        "flat filename required"
    );
    Ok(CString::new(s)?)
}
fn openat(fd: i32, s: &CString, flags: i32) -> Result<File> {
    let raw = unsafe {
        libc::openat(
            fd,
            s.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
            0o600 as libc::c_uint,
        )
    };
    ensure!(
        raw >= 0,
        "openat refused path: {}",
        std::io::Error::last_os_error()
    );
    Ok(unsafe { File::from_raw_fd(raw) })
}
fn absolute(p: &Path) -> Result<PathBuf> {
    ensure!(
        !p.components()
            .any(|c| matches!(c, Component::ParentDir | Component::Prefix(_))),
        "traversal refused"
    );
    Ok(if p.is_absolute() {
        p.into()
    } else {
        std::env::current_dir()?.join(p)
    })
}
impl Dir {
    fn exists(&self, n: &str) -> Result<bool> {
        let n = name(n)?;
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe {
            libc::fstatat(
                self.0.as_raw_fd(),
                n.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } == 0
        {
            return Ok(true);
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ENOENT) {
            return Ok(false);
        }
        Err(error.into())
    }
    fn open(p: &Path) -> Result<Self> {
        let mut d = Self(openat(
            libc::AT_FDCWD,
            &CString::new("/")?,
            libc::O_RDONLY | libc::O_DIRECTORY,
        )?);
        for c in absolute(p)?.components() {
            match c {
                Component::Normal(s) => {
                    d = Self(openat(
                        d.0.as_raw_fd(),
                        &name(s.to_str().context("UTF8 directory")?)?,
                        libc::O_RDONLY | libc::O_DIRECTORY,
                    )?)
                }
                Component::RootDir | Component::CurDir => (),
                _ => bail!("unsupported component"),
            }
        }
        Ok(d)
    }
    fn create(p: &Path) -> Result<Self> {
        let p = absolute(p)?;
        let parent = Self::open(p.parent().context("parent")?)?;
        let n = name(
            p.file_name()
                .context("new directory")?
                .to_str()
                .context("UTF8")?,
        )?;
        ensure!(
            unsafe { libc::mkdirat(parent.0.as_raw_fd(), n.as_ptr(), 0o700) } == 0,
            "case output must be a new directory"
        );
        parent.0.sync_all()?;
        Self::open(&p)
    }
    fn file(&self, n: &str, flags: i32) -> Result<File> {
        let f = openat(self.0.as_raw_fd(), &name(n)?, flags)?;
        ensure!(f.metadata()?.is_file(), "regular file required");
        Ok(f)
    }
    fn read(&self, n: &str, cap: usize) -> Result<Vec<u8>> {
        let f = self.file(n, libc::O_RDONLY)?;
        ensure!(f.metadata()?.len() <= cap as u64, "file exceeds cap");
        let mut bytes = vec![];
        f.take(cap as u64 + 1).read_to_end(&mut bytes)?;
        ensure!(bytes.len() <= cap, "file grew beyond cap");
        Ok(bytes)
    }
    fn write(&self, n: &str, bytes: &[u8]) -> Result<()> {
        let mut f = self.file(n, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        self.0.sync_all()?;
        Ok(())
    }
    fn executable(&self, n: &str) -> Result<()> {
        let f = self.file(n, libc::O_RDONLY)?;
        ensure!(
            unsafe { libc::fchmod(f.as_raw_fd(), 0o500) } == 0,
            "cannot set executable mode"
        );
        f.sync_all()?;
        Ok(())
    }
}
fn read_path(p: &Path, cap: usize) -> Result<Vec<u8>> {
    let p = absolute(p)?;
    Dir::open(p.parent().context("file parent")?)?.read(
        p.file_name()
            .context("file name")?
            .to_str()
            .context("UTF8")?,
        cap,
    )
}
fn sha(b: &[u8]) -> String {
    format!("{:x}", Sha256::digest(b))
}
fn id(b: &[u8]) -> Identity {
    Identity {
        sha256: sha(b),
        bytes: b.len() as u64,
    }
}
fn encode(v: &impl Serialize) -> Result<Vec<u8>> {
    Ok(serde_json::to_vec_pretty(v)?)
}
fn safe(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
}
fn digest(s: &str, len: usize) -> bool {
    s.len() == len
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn validate(p: &Profile, workload: &[u8]) -> Result<()> {
    ensure!(
        p.schema == "game_opt_loop/system_case_profile@2" && safe(&p.case_id),
        "unknown profile/unsafe case ID"
    );
    ensure!(
        matches!(p.transport.kind.as_str(), "adb" | "fake")
            && safe(&p.transport.target)
            && !p.transport.target.starts_with("hdc:"),
        "explicit Android transport required"
    );
    ensure!(
        contract::package(&p.package) && safe(&p.render_comm) && p.render_comm.len() <= 15,
        "valid Android package and exact Linux comm required"
    );
    ensure!(
        p.carrier.kind == "game_replay"
            && safe(&p.carrier.application_version)
            && safe(&p.carrier.scene_id)
            && p.carrier.reset_semantics == "caller_replay_no_exact_scene_reset",
        "explicit game replay context required; no installation or exact reset attestation"
    );
    ensure!(
        p.temperature_profile_id.as_deref() == Some("loaded_steady_v1")
            && p.goal.as_deref() == Some("render_cadence")
            && p.render_attribution_profile_id == contract::PROFILE
            && p.source_budget_calls == 0,
        "v2 requires loaded queued-origin cadence and zero model calls; no fallback"
    );
    ensure!(
        (2..=600).contains(&p.capture_seconds)
            && (1..=30).contains(&p.lead_seconds)
            && (5..=32).contains(&p.pairs)
            && p.paired_t_assumptions_acknowledged,
        "frozen sample/schedule assumptions required"
    );
    ensure!(
        safe(&p.control_id) && safe(&p.candidate_value),
        "unsafe candidate"
    );
    let b = &p.budgets;
    ensure!(
        (1..=7200).contains(&b.prepare_seconds)
            && (1..=7200).contains(&b.collect_seconds)
            && (1..=300).contains(&b.local_seconds)
            && b.whole_seconds >= b.prepare_seconds + b.collect_seconds + 2 * b.local_seconds
            && b.whole_seconds <= 18000
            && (1024..=8 * 1024 * 1024).contains(&b.stream_bytes),
        "finite stage/whole/output budgets required"
    );
    let w: Workload = serde_json::from_slice(workload)?;
    ensure!(
        w.schema == "phonefarm/deterministic_touch_workload@1"
            && (1..=4096).contains(&w.actions.len()),
        "invalid bounded workload"
    );
    let mut ms = 0u64;
    for a in w.actions {
        match a {
            Touch::Tap { x, y } => ensure!(x <= 16384 && y <= 16384, "tap out of bounds"),
            Touch::Swipe {
                x1,
                y1,
                x2,
                y2,
                duration_ms,
            } => {
                ensure!(
                    [x1, y1, x2, y2].iter().all(|v| *v <= 16384)
                        && (1..=10000).contains(&duration_ms),
                    "swipe out of bounds"
                );
                ms += duration_ms as u64;
            }
            Touch::Sleep { seconds } => {
                ensure!((1..=600).contains(&seconds), "sleep out of bounds");
                ms += seconds * 1000;
            }
        }
    }
    ensure!(
        ms >= (p.lead_seconds + p.capture_seconds + 3) * 1000 && ms <= 1200 * 1000,
        "workload must cover sampling window"
    );
    Ok(())
}
fn candidate(p: &Profile, probe: &[u8]) -> Result<Value> {
    let wl = sysparam::build_whitelist(&sysparam::parse_probe(std::str::from_utf8(probe)?));
    let spec = wl
        .get(&p.control_id)
        .context("selected control outside local proposal whitelist")?;
    ensure!(
        !spec.current.is_empty() && spec.current != p.candidate_value && spec.values.len() >= 2,
        "known original and a different candidate required"
    );
    let params = BTreeMap::from([(p.control_id.clone(), p.candidate_value.clone())]);
    sysparam::validate(&params, &wl).map_err(anyhow::Error::msg)?;
    Ok(
        json!({"kind":"sysparam","payload":{"why":"deterministic explicit single-control choice; receiver qualification required","params":params,"plan":sysparam::plan(&params,&wl),"deny_keywords":sysparam::DENY_KEYWORDS}}),
    )
}
fn request(p: &Profile, root: &Path) -> Value {
    let mut v = json!({"schema":"phonefarm/confirmation_driver_request@1","transport":{"kind":p.transport.kind,"program":root.join("transport-tool"),"target":p.transport.target},"package":p.package,"render_comm":p.render_comm,"capture_seconds":p.capture_seconds,"lead_seconds":p.lead_seconds,"workload":root.join("workload.json"),"candidate":root.join("candidate.json"),"schedule":(0..p.pairs).map(|i|json!({"pair_id":format!("{}_p{i:02}",p.case_id),"a_run_id":format!("{}_a{i:02}",p.case_id),"b_run_id":format!("{}_b{i:02}",p.case_id),"baseline_first":i%2==0})).collect::<Vec<_>>(),"paired_t_assumptions_acknowledged":p.paired_t_assumptions_acknowledged});
    if let Some(s) = &p.temperature_profile_id {
        v["temperature_profile_id"] = json!(s);
    }
    if let Some(s) = &p.goal {
        v["goal"] = json!(s);
    }
    v["render_attribution_profile_id"] = json!(p.render_attribution_profile_id);
    v["render_reader_artifact"] = json!(root.join("native-reader"));
    v
}
fn plan(profile: &Path, out: &Path) -> Result<Value> {
    let pb = read_path(profile, JSON_CAP)?;
    let p: Profile = serde_json::from_slice(&pb)?;
    let workload = read_path(&p.workload, JSON_CAP)?;
    let probe = read_path(&p.probe, JSON_CAP)?;
    validate(&p, &workload)?;
    let cand = encode(&candidate(&p, &probe)?)?;
    let tool = read_path(&p.transport.program, TOOL_CAP)?;
    let out = absolute(out)?;
    let req = encode(&request(&p, &out))?;
    let files = BTreeMap::from([
        ("profile.json", pb),
        ("probe.txt", probe),
        ("workload.json", workload),
        ("candidate.json", cand),
        ("request.json", req),
        ("transport-tool", tool),
    ]);
    let mut files = files
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect::<BTreeMap<_, _>>();
    files.extend(contract::artifact(&p.render_reader_artifact)?);
    let manifest = Manifest {
        schema: "game_opt_loop/system_case@2".into(),
        files: files.iter().map(|(k, v)| (k.to_string(), id(v))).collect(),
        implementation_sha256: contract::implementation_hash(),
        candidate_generator_sha256: sha(include_str!("optimize/sysparam.rs").as_bytes()),
        carrier_context: "caller_declared_not_launch_attestation".into(),
        real_game_optimization_proven: false,
        promotion_allowed: false,
    };
    let d = Dir::create(&out)?;
    let native = Dir::create(&out.join("native-reader"))?;
    for (n, b) in files {
        if let Some(n) = n.strip_prefix("native-reader/") {
            native.write(n, &b)?;
        } else {
            d.write(&n, &b)?;
        }
    }
    d.executable("transport-tool")?;
    d.write("manifest.json", &encode(&manifest)?)?;
    Ok(
        json!({"status":"PLANNED","case":out,"device_executed":false,"candidate_source":"deterministic_explicit_choice","carrier_context":"caller_declared_not_launch_attestation","real_game_optimization_proven":false,"promotion_allowed":false}),
    )
}
fn verify(root: &Path) -> Result<(Dir, Profile)> {
    let d = Dir::open(root)?;
    let m: Manifest = serde_json::from_slice(&d.read("manifest.json", JSON_CAP)?)?;
    ensure!(
        m.schema == "game_opt_loop/system_case@2"
            && !m.real_game_optimization_proven
            && !m.promotion_allowed
            && m.carrier_context == "caller_declared_not_launch_attestation"
            && m.implementation_sha256 == contract::implementation_hash()
            && m.candidate_generator_sha256 == sha(include_str!("optimize/sysparam.rs").as_bytes()),
        "case contract/proof/implementation mismatch"
    );
    let expected = [
        "candidate.json",
        "native-reader/build.json",
        "native-reader/compiler.stderr",
        "native-reader/compiler.stdout",
        "native-reader/reader.elf",
        "native-reader/source.c",
        "probe.txt",
        "profile.json",
        "request.json",
        "transport-tool",
        "workload.json",
    ];
    ensure!(
        m.files.keys().map(String::as_str).eq(expected),
        "case file set mismatch"
    );
    let mut files = BTreeMap::new();
    for (n, h) in m.files {
        let b = read_path(
            &root.join(&n),
            if n == "transport-tool" {
                TOOL_CAP
            } else if n == "native-reader/reader.elf" {
                1024 * 1024
            } else {
                JSON_CAP
            },
        )?;
        ensure!(id(&b) == h, "frozen file mismatch: {n}");
        files.insert(n, b);
    }
    contract::validate_artifact(&files)?;
    contract::artifact(&root.join("native-reader"))?;
    let p: Profile = serde_json::from_slice(&files["profile.json"])?;
    validate(&p, &files["workload.json"])?;
    ensure!(
        files["candidate.json"] == encode(&candidate(&p, &files["probe.txt"])?)?
            && files["request.json"] == encode(&request(&p, root))?,
        "frozen deterministic candidate/request mismatch"
    );
    Ok((d, p))
}
fn copy_tool(source: &Path, d: &Dir, name: &str, start: Instant, wall: u64) -> Result<Identity> {
    let p = absolute(source)?;
    let mut f = Dir::open(p.parent().context("tool parent")?)?.file(
        p.file_name()
            .context("tool file")?
            .to_str()
            .context("UTF8 tool")?,
        libc::O_RDONLY,
    )?;
    let size = f.metadata()?.len();
    ensure!(size <= EXEC_CAP, "tool byte cap");
    let mut out = d.file(name, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
    let mut h = Sha256::new();
    let mut total = 0u64;
    let mut buf = [0; 65536];
    loop {
        ensure!(
            start.elapsed() < Duration::from_secs(wall),
            "whole budget during tool snapshot"
        );
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        };
        total += n as u64;
        ensure!(total <= EXEC_CAP, "tool grew beyond byte cap");
        h.update(&buf[..n]);
        out.write_all(&buf[..n])?;
    }
    ensure!(
        total == size && f.metadata()?.len() == size,
        "tool size changed during snapshot"
    );
    out.sync_all()?;
    d.executable(name)?;
    d.0.sync_all()?;
    Ok(Identity {
        sha256: format!("{:x}", h.finalize()),
        bytes: total,
    })
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Output {
    stdout_sha256: String,
    stderr_sha256: String,
    code: Option<i32>,
    signal: Option<i32>,
    elapsed_ms: u128,
    host_group_absent: bool,
    state: String,
    error: Option<String>,
    real_game_optimization_proven: bool,
    promotion_allowed: bool,
}
fn absent(pid: u32) -> bool {
    (unsafe { libc::kill(-(pid as i32), 0) }) < 0
        && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
}
fn drain(pipe: &mut impl Read, bytes: &mut Vec<u8>, cap: usize) -> Result<bool> {
    let mut buf = [0; 8192];
    for _ in 0..16 {
        match pipe.read(&mut buf) {
            Ok(0) => return Ok(true),
            Ok(n) => {
                let room = cap - bytes.len();
                bytes.extend_from_slice(&buf[..n.min(room)]);
                ensure!(n <= room, "stream byte cap exceeded");
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return Ok(false),
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => (),
            Err(e) => return Err(e.into()),
        }
    }
    Ok(false)
}
fn nonblocking(fd: i32) -> Result<()> {
    let f = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    ensure!(
        f >= 0 && unsafe { libc::fcntl(fd, libc::F_SETFL, f | libc::O_NONBLOCK) } >= 0,
        "nonblocking output failed"
    );
    Ok(())
}
struct Owned(Child);
impl Drop for Owned {
    fn drop(&mut self) {
        if !absent(self.0.id()) {
            unsafe { libc::kill(-(self.0.id() as i32), libc::SIGKILL) };
        }
        let stop = Instant::now();
        while stop.elapsed() < Duration::from_secs(2) {
            match self.0.try_wait() {
                Ok(Some(_)) | Err(_) => break,
                Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            }
        }
    }
}
fn execute(
    d: &Dir,
    index: usize,
    exe: &Path,
    args: &[String],
    wall: Duration,
    cap: usize,
) -> Result<(Output, Vec<u8>)> {
    let prefix = format!("{index:02}-{}", PHASES[index]);
    d.write(&format!("{prefix}.intent.json"),&encode(&json!({"state":"IN_FLIGHT","program":exe,"argv":args,"at":chrono::Utc::now().to_rfc3339(),"host_budget_ms":wall.as_millis(),"device_idle_proven":false}))?)?;
    let begin = Instant::now();
    let mut o = Output {
        stdout_sha256: sha(b""),
        stderr_sha256: sha(b""),
        code: None,
        signal: None,
        elapsed_ms: 0,
        host_group_absent: false,
        state: "UNKNOWN".into(),
        error: None,
        real_game_optimization_proven: false,
        promotion_allowed: false,
    };
    let mut stdout = vec![];
    let mut stderr = vec![];
    match Command::new(exe)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
    {
        Err(e) => {
            o.state = "FAILED_BEFORE_SPAWN".into();
            o.error = Some(e.to_string());
            o.host_group_absent = true;
        }
        Ok(child) => {
            let mut owned = Owned(child);
            let pid = owned.0.id();
            let result = (|| -> Result<()> {
                d.write(
                    &format!("{prefix}.pid.json"),
                    &encode(&json!({"pid":pid,"pgid":pid}))?,
                )?;
                let mut op = owned.0.stdout.take().context("stdout")?;
                let mut ep = owned.0.stderr.take().context("stderr")?;
                nonblocking(op.as_raw_fd())?;
                nonblocking(ep.as_raw_fd())?;
                let (mut od, mut ed) = (false, false);
                let mut status = None;
                loop {
                    if !od {
                        od = drain(&mut op, &mut stdout, cap)?
                    };
                    if !ed {
                        ed = drain(&mut ep, &mut stderr, cap)?
                    };
                    if status.is_none() {
                        status = owned.0.try_wait()?
                    };
                    ensure!(begin.elapsed() < wall, "stage/whole wall budget exceeded");
                    if let Some(s) = status {
                        o.code = s.code();
                        o.signal = s.signal();
                        if od && ed {
                            ensure!(
                                absent(pid),
                                "closed pipes but host group still live/unknown"
                            );
                            ensure!(s.signal().is_none(), "host tool ended by signal");
                            return Ok(());
                        }
                    }
                    std::thread::sleep(Duration::from_millis(5));
                }
            })();
            if let Err(e) = result {
                o.error = Some(format!("{e:#}"));
            } else {
                o.state = "RETURNED".into();
            }
            drop(owned);
            o.host_group_absent = absent(pid);
        }
    }
    o.elapsed_ms = begin.elapsed().as_millis();
    o.stdout_sha256 = sha(&stdout);
    o.stderr_sha256 = sha(&stderr);
    d.write(&format!("{prefix}.stdout"), &stdout)?;
    d.write(&format!("{prefix}.stderr"), &stderr)?;
    d.write(&format!("{prefix}.result.json"), &encode(&o)?)?;
    Ok((o, stdout))
}
fn stage_ok(o: &Output) -> Result<()> {
    ensure!(
        o.state == "RETURNED" && o.host_group_absent && o.code == Some(0) && o.signal.is_none(),
        "stage stopped: {:?}/{}",
        o.error,
        o.state
    );
    Ok(())
}
fn proof_false(v: &Value) -> bool {
    v["real_game_optimization_proven"] == false && v["promotion_allowed"] == false
}
fn validate_receiver_plan(root: &Path, p: &Profile) -> Result<()> {
    let receiver = root.join("receiver");
    let v: Value = serde_json::from_slice(&read_path(&receiver.join("plan.json"), JSON_CAP)?)?;
    let requested = request(p, root);
    let mut metrics = json!([{"name":"frame_p95_ms","direction":"lower","scale":"percent","minimum_improvement":1.0,"maximum_regression":1.0},{"name":"fps_mean","direction":"higher","scale":"percent","minimum_improvement":null,"maximum_regression":1.0},{"name":"power_watt","direction":"lower","scale":"percent","minimum_improvement":null,"maximum_regression":1.0}]);
    if p.goal.as_deref() == Some("render_cadence") {
        metrics.as_array_mut().expect("fixed metric array").pop();
    }
    if let Some(goal) = &p.goal {
        let raw = read_path(&receiver.join("conditions.json"), JSON_CAP)?;
        ensure!(
            v["conditions_sha256"] == sha(&raw),
            "explicit goal conditions bytes differ from receiver plan"
        );
        let c: Value = serde_json::from_slice(&raw)?;
        let required = if goal == "render_cadence" {
            json!(["frame_p95", "fps_mean"])
        } else {
            json!(["frame_p95", "fps_mean", "power_w_mean"])
        };
        ensure!(
            c["policy"]["goal"] == *goal && c["policy"]["required_metrics"] == required,
            "receiver goal/required metrics differ from explicit case request"
        );
    }
    ensure!(
        v["schedule"] == requested["schedule"]
            && v["pairs"] == p.pairs
            && v["family_alpha"] == 0.05
            && v["paired_t_assumptions_acknowledged"] == true
            && v["metrics"] == metrics,
        "receiver confirmation policy/schedule differs from v2 case contract"
    );
    for (key, file) in [
        ("candidate_artifact_sha256", "candidate.json"),
        ("baseline_artifact_sha256", "baseline.json"),
        ("conditions_sha256", "conditions.json"),
        ("baseline_admission_sha256", "admission.json"),
    ] {
        let raw = read_path(&receiver.join(file), JSON_CAP)?;
        ensure!(
            v[key] == sha(&raw),
            "receiver plan does not bind actual {file} bytes"
        );
        if file == "candidate.json" {
            ensure!(
                raw == read_path(&root.join(file), JSON_CAP)?,
                "receiver candidate differs from frozen GOL candidate"
            );
        }
    }
    ensure!(
        v["exploration_run_ids"] == json!(["admission_0", "admission_1", "admission_2"]),
        "receiver admission IDs unknown"
    );
    contract::receiver_plan(root, p)?;
    Ok(())
}
fn recorded_artifacts(root: &Path) -> Result<BTreeMap<String, Identity>> {
    let mut files = BTreeMap::new();
    for n in [
        "manifest.json",
        "tools.json",
        "frozen.json",
        "report.json",
        "receiver/plan.json",
        "receiver/samples.json",
    ] {
        files.insert(
            n.to_owned(),
            id(&read_path(&root.join(n), contract::artifact_cap(n))?),
        );
    }
    for n in contract::receiver_artifacts(root)? {
        files.insert(
            n.clone(),
            id(&read_path(&root.join(&n), contract::artifact_cap(&n))?),
        );
    }
    for (i, phase) in PHASES.iter().enumerate() {
        for suffix in ["stdout", "stderr", "result.json"] {
            let n = format!("{i:02}-{phase}.{suffix}");
            files.insert(n.clone(), id(&read_path(&root.join(n), 8 * 1024 * 1024)?));
        }
    }
    Ok(files)
}
fn validate_recorded_stages(root: &Path, p: &Profile) -> Result<()> {
    let d = Dir::open(root)?;
    let receiver = root.join("receiver");
    for (i, phase) in PHASES.iter().enumerate() {
        let prefix = format!("{i:02}-{phase}");
        let o: Output =
            serde_json::from_slice(&d.read(&format!("{prefix}.result.json"), JSON_CAP)?)?;
        let raw = d.read(&format!("{prefix}.stdout"), p.budgets.stream_bytes)?;
        ensure!(
            !o.real_game_optimization_proven
                && !o.promotion_allowed
                && o.stdout_sha256 == sha(&raw)
                && o.stderr_sha256
                    == sha(&d.read(&format!("{prefix}.stderr"), p.budgets.stream_bytes)?),
            "completed stage raw/proof binding mismatch"
        );
        let reply: Value = serde_json::from_slice(&raw)?;
        if i == 3 {
            let report: Value = serde_json::from_slice(&d.read("report.json", JSON_CAP)?)?;
            ensure!(
                o.state == "RETURNED"
                    && o.host_group_absent
                    && o.signal.is_none()
                    && reply == report
                    && report["schema"] == "game_opt_loop/confirmation_report@1"
                    && proof_false(&report)
                    && ((report["verdict"] == "PASS" && o.code == Some(0))
                        || (matches!(
                            report["verdict"].as_str(),
                            Some("INCONCLUSIVE" | "BLOCKED")
                        ) && o.code == Some(1))),
                "recorded judgment stdout/report/exit mismatch"
            );
        } else {
            stage_ok(&o)?;
            if i == 1 {
                let frozen: Value = serde_json::from_slice(&d.read("frozen.json", JSON_CAP)?)?;
                ensure!(
                    reply == frozen && frozen["schema"] == "game_opt_loop/confirmation_plan@1",
                    "recorded freeze stdout/plan mismatch"
                );
            } else {
                let (status, key, n) = if i == 0 {
                    ("PREPARED", "plan", "plan.json")
                } else {
                    ("COLLECTED_OBSERVATIONS", "samples", "samples.json")
                };
                ensure!(
                    p.transport.kind == "adb"
                        && reply["status"] == status
                        && reply["evidence_origin"] == "live_device"
                        && reply["eligible"] == false
                        && proof_false(&reply)
                        && reply[key] == receiver.join(n).to_str().context("UTF8 receiver path")?,
                    "recorded receiver status/source/output mismatch"
                );
            }
        }
    }
    validate_receiver_plan(root, p)?;
    contract::receiver_samples(root, p)?;
    Ok(())
}
fn deadline(start: Instant, seconds: u64) -> Result<()> {
    ensure!(
        start.elapsed() < Duration::from_secs(seconds),
        "whole case budget exceeded before final publication"
    );
    Ok(())
}
fn run_case(root: &Path, pf: &Path, execute_requested: bool) -> Result<Value> {
    let root = absolute(root)?;
    let (d, p) = verify(&root)?;
    if !execute_requested {
        return Ok(
            json!({"status":"VALIDATED_ONLY","device_executed":false,"real_game_optimization_proven":false,"promotion_allowed":false}),
        );
    }
    d.write("run-start.json",&encode(&json!({"state":"STARTED","at":chrono::Utc::now().to_rfc3339(),"receiver_session":root.join("receiver"),"budgets":p.budgets,"no_auto_retry":true}))?)?;
    let start = Instant::now();
    let result = (|| -> Result<Value> {
        let pfid = copy_tool(pf, &d, "phonefarm-tool", start, p.budgets.whole_seconds)?;
        let golid = copy_tool(
            &std::env::current_exe()?,
            &d,
            "gol-tool",
            start,
            p.budgets.whole_seconds,
        )?;
        d.write("tools.json",&encode(&json!({"phonefarm":pfid,"gol":golid,"source_attestation":"trusted_local_bytes_not_hardware_attestation"}))?)?;
        let receiver = root.join("receiver");
        let frozen = root.join("frozen.json");
        let report = root.join("report.json");
        let path =
            |p: &Path| -> Result<String> { Ok(p.to_str().context("UTF8 argv path")?.to_owned()) };
        let phases = [
            (
                "phonefarm-tool",
                vec![
                    "confirmation-driver".into(),
                    "prepare".into(),
                    "--request".into(),
                    path(&root.join("request.json"))?,
                    "--out".into(),
                    path(&receiver)?,
                    "--execute".into(),
                ],
                p.budgets.prepare_seconds,
            ),
            (
                "gol-tool",
                vec![
                    "confirm".into(),
                    "freeze".into(),
                    "--plan".into(),
                    path(&receiver.join("plan.json"))?,
                    "--out".into(),
                    path(&frozen)?,
                ],
                p.budgets.local_seconds,
            ),
            (
                "phonefarm-tool",
                vec![
                    "confirmation-driver".into(),
                    "collect".into(),
                    "--session".into(),
                    path(&receiver)?,
                    "--frozen".into(),
                    path(&frozen)?,
                    "--execute".into(),
                ],
                p.budgets.collect_seconds,
            ),
            (
                "gol-tool",
                vec![
                    "confirm".into(),
                    "judge".into(),
                    "--frozen".into(),
                    path(&frozen)?,
                    "--samples".into(),
                    path(&receiver.join("samples.json"))?,
                    "--out".into(),
                    path(&report)?,
                ],
                p.budgets.local_seconds,
            ),
        ];
        for (i, (tool, args, seconds)) in phases.iter().enumerate() {
            verify(&root)?;
            let remaining = Duration::from_secs(p.budgets.whole_seconds)
                .checked_sub(start.elapsed())
                .context("whole case budget exhausted")?;
            let (o, stdout) = execute(
                &d,
                i,
                &root.join(tool),
                args,
                Duration::from_secs(*seconds).min(remaining),
                p.budgets.stream_bytes,
            )?;
            if i == 3 {
                ensure!(
                    o.state == "RETURNED" && o.host_group_absent && o.signal.is_none(),
                    "judge execution unknown"
                );
                let raw = read_path(&report, JSON_CAP)?;
                let v: Value = serde_json::from_slice(&raw)?;
                ensure!(
                    v["schema"] == "game_opt_loop/confirmation_report@1"
                        && proof_false(&v)
                        && matches!(
                            v["verdict"].as_str(),
                            Some("PASS" | "INCONCLUSIVE" | "BLOCKED")
                        )
                        && ((v["verdict"] == "PASS" && o.code == Some(0))
                            || (v["verdict"] != "PASS" && o.code == Some(1))),
                    "judge report/exit/proof mismatch"
                );
                ensure!(
                    serde_json::from_slice::<Value>(&stdout)? == v,
                    "judge stdout differs from saved report"
                );
                validate_recorded_stages(&root, &p)?;
                let artifacts = recorded_artifacts(&root)?;
                deadline(start, p.budgets.whole_seconds)?;
                return Ok(
                    json!({"status":"CASE_RECORDED","verdict":v["verdict"],"case":root,"receiver_session":receiver,"confirmation_report_sha256":sha(&raw),"artifacts":artifacts,"whole_budget_elapsed_ms":start.elapsed().as_millis(),"source_attestation":"recorded_receiver_claims_not_authenticated_acquisition","real_game_optimization_proven":false,"promotion_allowed":false}),
                );
            }
            stage_ok(&o)?;
            let v: Value = serde_json::from_slice(&stdout)?;
            if i == 0 || i == 2 {
                ensure!(
                    p.transport.kind == "adb"
                        && v["evidence_origin"] == "live_device"
                        && proof_false(&v)
                        && v["eligible"] == false,
                    "synthetic/unknown receiver cannot advance real case"
                );
                let (expected, key, output) = if i == 0 {
                    ("PREPARED", "plan", receiver.join("plan.json"))
                } else {
                    (
                        "COLLECTED_OBSERVATIONS",
                        "samples",
                        receiver.join("samples.json"),
                    )
                };
                ensure!(
                    v["status"] == expected && v[key] == path(&output)?,
                    "receiver status/path not the frozen case output"
                );
                read_path(&output, JSON_CAP)?;
                if i == 0 {
                    validate_receiver_plan(&root, &p)?;
                } else {
                    contract::receiver_samples(&root, &p)?;
                }
            } else {
                ensure!(
                    serde_json::from_slice::<Value>(&read_path(&frozen, JSON_CAP)?)? == v,
                    "freeze stdout differs from frozen plan"
                );
            }
            d.write(
                &format!("{i:02}-{}.accepted.json", PHASES[i]),
                &encode(&json!({"state":"ACCEPTED","reply_sha256":sha(&stdout)}))?,
            )?;
        }
        bail!("case missing final judgment")
    })();
    match result {
        Ok(v) => {
            deadline(start, p.budgets.whole_seconds)?;
            d.write("case-result.json", &encode(&v)?)?;
            if deadline(start, p.budgets.whole_seconds).is_err() {
                let late = json!({"status":"STOPPED","state":"UNKNOWN","error":"whole budget exceeded during final publication","device_idle_proven":false,"real_game_optimization_proven":false,"promotion_allowed":false});
                d.write("publication-overrun.json", &encode(&late)?)?;
                return Ok(late);
            }
            Ok(v)
        }
        Err(e) => {
            let v = json!({"status":"STOPPED","error":format!("{e:#}"),"receiver_session":root.join("receiver"),"recovery":"Never rerun this case. Inspect PF persistent barrier; device owner explicitly reconciles the exact receiver session where supported, then creates a new case. Host stop is not remote/device-idle proof.","real_game_optimization_proven":false,"promotion_allowed":false});
            d.write("case-result.json", &encode(&v)?)?;
            Ok(v)
        }
    }
}
fn inspect(root: &Path) -> Result<Value> {
    let root = absolute(root)?;
    let (d, profile) = verify(&root)?;
    let mut stages = vec![];
    for (i, p) in PHASES.iter().enumerate() {
        let prefix = format!("{i:02}-{p}");
        if d.exists(&format!("{prefix}.intent.json"))? {
            let b = d.read(&format!("{prefix}.intent.json"), JSON_CAP)?;
            let intent: Value = serde_json::from_slice(&b)?;
            let result = if d.exists(&format!("{prefix}.result.json"))? {
                let bytes = d.read(&format!("{prefix}.result.json"), JSON_CAP)?;
                let o: Output = serde_json::from_slice(&bytes)?;
                ensure!(
                    !o.real_game_optimization_proven && !o.promotion_allowed,
                    "unexpected proof flags"
                );
                ensure!(
                    sha(&d.read(&format!("{prefix}.stdout"), 8 * 1024 * 1024)?) == o.stdout_sha256
                        && sha(&d.read(&format!("{prefix}.stderr"), 8 * 1024 * 1024)?)
                            == o.stderr_sha256,
                    "raw stage output hash mismatch"
                );
                serde_json::to_value(o)?
            } else {
                json!({"state":"UNKNOWN","reason":"durable intent without readable complete result; never retry"})
            };
            stages.push(json!({"stage":p,"intent":intent,"result":result}));
        } else {
            stages.push(json!({"stage":p,"state":"NOT_RECORDED"}));
        }
    }
    let mut final_result = if d.exists("case-result.json")? {
        let b = d.read("case-result.json", JSON_CAP)?;
        let v: Value = serde_json::from_slice(&b)?;
        ensure!(proof_false(&v), "unexpected final proof flags");
        if v["status"] == "CASE_RECORDED" {
            validate_recorded_stages(&root, &profile)?;
            ensure!(
                v["whole_budget_elapsed_ms"]
                    .as_u64()
                    .is_some_and(|ms| ms < profile.budgets.whole_seconds * 1000),
                "recorded final result outside whole budget"
            );
            ensure!(
                v["artifacts"] == serde_json::to_value(recorded_artifacts(&root)?)?,
                "final artifact hashes differ from current recorded bytes"
            );
            let report: Value =
                serde_json::from_slice(&read_path(&root.join("report.json"), JSON_CAP)?)?;
            ensure!(
                v["verdict"] == report["verdict"]
                    && proof_false(&report)
                    && v["confirmation_report_sha256"]
                        == sha(&read_path(&root.join("report.json"), JSON_CAP)?),
                "final report/verdict mismatch"
            );
        } else {
            ensure!(v["status"] == "STOPPED", "unknown final state");
        }
        Some(v)
    } else {
        None
    };
    let publication_error = if d.exists("publication-overrun.json")? {
        let error: Value = serde_json::from_slice(&d.read("publication-overrun.json", JSON_CAP)?)?;
        ensure!(
            proof_false(&error) && error["status"] == "STOPPED" && error["state"] == "UNKNOWN",
            "invalid publication error marker"
        );
        final_result = Some(error.clone());
        Some(error)
    } else {
        None
    };
    let execution_state = if publication_error.is_some() {
        "UNKNOWN"
    } else if final_result.is_some() {
        "RECORDED"
    } else if d.exists("run-start.json")? {
        "UNKNOWN"
    } else {
        "PLANNED"
    };
    Ok(
        json!({"status":"INSPECTED","execution_state":execution_state,"case":root,"stages":stages,"result":final_result,"publication_error":publication_error,"resumption_allowed":false,"real_game_optimization_proven":false,"promotion_allowed":false}),
    )
}
pub fn run(action: Action) -> Result<()> {
    let (file, is_profile) = match &action {
        Action::Plan { profile, .. } => (profile.clone(), true),
        Action::Run { case, .. } | Action::Inspect { case } => (case.join("manifest.json"), false),
    };
    let wire: Value = serde_json::from_slice(&read_path(&file, JSON_CAP)?)?;
    let legacy = if is_profile {
        "game_opt_loop/system_case_profile@1"
    } else {
        "game_opt_loop/system_case@1"
    };
    if wire["schema"] == legacy {
        return crate::system_case::run(match action {
            Action::Plan { profile, out } => crate::system_case::Action::Plan { profile, out },
            Action::Run {
                case,
                phonefarm_bin,
                execute,
            } => crate::system_case::Action::Run {
                case,
                phonefarm_bin,
                execute,
            },
            Action::Inspect { case } => crate::system_case::Action::Inspect { case },
        });
    }
    let v = match action {
        Action::Plan { profile, out } => plan(&profile, &out)?,
        Action::Run {
            case,
            phonefarm_bin,
            execute,
        } => run_case(&case, &phonefarm_bin, execute)?,
        Action::Inspect { case } => inspect(&case)?,
    };
    println!("{}", serde_json::to_string(&v)?);
    ensure!(
        v["status"] != "STOPPED",
        "single case stopped; raw evidence retained; no automatic retry"
    );
    Ok(())
}
