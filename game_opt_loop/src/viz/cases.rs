//! Finite saved-schema reader. No producer imports, execution or qualification grants.
use serde::{
    de::{self, MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer, Serialize,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    fs::File,
    io::Read,
    path::{Component, Path, PathBuf},
};

const CONFIRM: &str = "game_opt_loop/confirmation_report@1";
const MODEL: &str = "game_opt_loop/model_pipeline_journal@1";
const MAX_FILE: usize = 2 * 1024 * 1024;
const MAX_TOTAL: usize = 16 * 1024 * 1024;
const MAX_CASES: usize = 64;
const MAX_DIRS: usize = 256;
const MAX_ENTRIES: usize = 4096;
const MAX_DEPTH: usize = 5;
const MAX_ASSOCIATIONS: usize = 32;

#[derive(Debug, Clone, Serialize)]
pub struct Collection {
    pub items: Vec<Case>,
    /// Exactly one owned raw/parsed snapshot per retained path, not one copy per link.
    pub documents: Vec<Document>,
    pub problems: Vec<String>,
    pub partial: bool,
    pub scanned_directories: usize,
    pub scanned_entries: usize,
    pub snapshot_bytes: usize,
    pub view_grants_game_proof: bool,
    pub view_allows_promotion: bool,
}
#[derive(Debug, Clone, Serialize)]
pub struct Document {
    pub path: String,
    pub sha256: String,
    pub size_bytes: usize,
    /// Exact UTF-8 bytes read, including original whitespace. Not pretty Value output.
    pub raw_text: Option<String>,
    pub json: Option<Value>,
    pub problem: Option<String>,
}
#[derive(Debug, Clone, Serialize)]
pub struct Association {
    pub document: usize,
    pub role: String,
    pub integrity: String,
}
#[derive(Debug, Clone, Serialize)]
pub struct Stage {
    pub name: Option<String>,
    pub state: Option<String>,
    pub error: Option<String>,
    pub decisions: Option<Value>,
    pub reconciliation: Option<Value>,
    pub usage: Option<Value>,
}
#[derive(Debug, Clone, Serialize)]
pub struct Case {
    pub id: String,
    pub kind: &'static str,
    pub title: Option<String>,
    pub declared_status: Option<String>,
    pub source_declarations: Vec<String>,
    pub qualification: Option<Value>,
    pub scope: Option<String>,
    pub issues: Vec<String>,
    pub stages: Vec<Stage>,
    pub primary_document: usize,
    pub associations: Vec<Association>,
    pub view_grants_game_proof: bool,
    pub view_allows_promotion: bool,
}

fn text(v: &Value, key: &str) -> Option<String> {
    v.get(key)?.as_str().map(str::to_owned)
}
fn digest_ok(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn strings(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

// serde_json::Value normally replaces duplicate keys. A displayed snapshot must not
// turn ambiguous JSON into an apparently ordinary declaration.
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Unique;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("JSON without duplicate keys")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Unique, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| Unique(Value::Number(n)))
                    .ok_or_else(|| E::custom("nonfinite JSON"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut out = Vec::new();
                while let Some(Unique(v)) = a.next_element()? {
                    out.push(v);
                }
                Ok(Unique(Value::Array(out)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut out = serde_json::Map::new();
                while let Some(k) = a.next_key::<String>()? {
                    if out.contains_key(&k) {
                        return Err(de::Error::custom(format!("duplicate JSON key: {k}")));
                    }
                    let Unique(v) = a.next_value()?;
                    out.insert(k, v);
                }
                Ok(Unique(Value::Object(out)))
            }
        }
        d.deserialize_any(V)
    }
}

#[cfg(unix)]
mod bounded {
    use super::*;
    use std::{
        ffi::{CStr, CString, OsString},
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::{
                ffi::{OsStrExt, OsStringExt},
                fs::OpenOptionsExt,
            },
        },
    };
    pub struct Root {
        pub path: PathBuf,
        file: File,
    }
    impl Root {
        pub fn open(path: &Path) -> Result<Self, String> {
            let absolute = if path.is_absolute() {
                path.to_owned()
            } else {
                std::env::current_dir()
                    .map_err(|e| e.to_string())?
                    .join(path)
            };
            let parts = components(absolute.strip_prefix("/").map_err(|e| e.to_string())?)?;
            // Anchor the configured root component by component. Never canonicalize
            // after opening a fd, or re-open an association through a path alias.
            let mut file = std::fs::OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_NONBLOCK)
                .open("/")
                .map_err(|e| e.to_string())?;
            for part in parts {
                let name = CString::new(part.as_bytes()).map_err(|_| "NUL root path")?;
                let fd = unsafe {
                    libc::openat(
                        file.as_raw_fd(),
                        name.as_ptr(),
                        libc::O_RDONLY
                            | libc::O_CLOEXEC
                            | libc::O_DIRECTORY
                            | libc::O_NOFOLLOW
                            | libc::O_NONBLOCK,
                    )
                };
                if fd < 0 {
                    return Err(std::io::Error::last_os_error().to_string());
                }
                file = unsafe { File::from_raw_fd(fd) };
            }
            if !file.metadata().map_err(|e| e.to_string())?.is_dir() {
                return Err("root is not a directory".into());
            }
            Ok(Self {
                path: absolute,
                file,
            })
        }
        fn open_relative(&self, relative: &Path, directory: bool) -> Result<File, String> {
            let parts = components(relative)?;
            let mut current = self.file.try_clone().map_err(|e| e.to_string())?;
            if parts.is_empty() {
                return if directory {
                    Ok(current)
                } else {
                    Err("file path required".into())
                };
            }
            let n = parts.len();
            for (i, p) in parts.into_iter().enumerate() {
                let name = CString::new(p.as_bytes()).map_err(|_| "NUL path")?;
                let dir = i + 1 < n || directory;
                let flags = libc::O_RDONLY
                    | libc::O_CLOEXEC
                    | libc::O_NOFOLLOW
                    | libc::O_NONBLOCK
                    | if dir { libc::O_DIRECTORY } else { 0 };
                let fd = unsafe { libc::openat(current.as_raw_fd(), name.as_ptr(), flags) };
                if fd < 0 {
                    return Err(std::io::Error::last_os_error().to_string());
                }
                current = unsafe { File::from_raw_fd(fd) };
                let meta = current.metadata().map_err(|e| e.to_string())?;
                if (dir && !meta.is_dir()) || (!dir && !meta.is_file()) {
                    return Err("regular file/directory required".into());
                }
            }
            Ok(current)
        }
        pub fn names(
            &self,
            relative: &Path,
            remaining: usize,
        ) -> Result<(Vec<OsString>, bool), String> {
            let f = self.open_relative(relative, true)?;
            let fd = unsafe { libc::dup(f.as_raw_fd()) };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().to_string());
            }
            let dir = unsafe { libc::fdopendir(fd) };
            if dir.is_null() {
                unsafe { libc::close(fd) };
                return Err(std::io::Error::last_os_error().to_string());
            }
            struct Dir(*mut libc::DIR);
            impl Drop for Dir {
                fn drop(&mut self) {
                    unsafe { libc::closedir(self.0) };
                }
            }
            let d = Dir(dir);
            let mut names = Vec::new();
            let mut limited = false;
            loop {
                errno::set_errno(errno::Errno(0));
                let entry = unsafe { libc::readdir(d.0) };
                if entry.is_null() {
                    let error = errno::errno().0;
                    if error != 0 {
                        return Err(std::io::Error::from_raw_os_error(error).to_string());
                    }
                    break;
                }
                let b = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
                if b == b"." || b == b".." {
                    continue;
                }
                if names.len() == remaining {
                    limited = true;
                    break;
                }
                names.push(OsString::from_vec(b.to_vec()));
            }
            names.sort();
            Ok((names, limited))
        }
        pub fn is_dir(&self, p: &Path) -> bool {
            self.open_relative(p, true).is_ok()
        }
        pub fn bytes(&self, p: &Path, limit: usize) -> Result<Vec<u8>, String> {
            let f = self.open_relative(p, false)?;
            if f.metadata().map_err(|e| e.to_string())?.len() > limit as u64 {
                return Err("JSON byte limit reached".into());
            }
            let mut bytes = Vec::new();
            f.take((limit + 1) as u64)
                .read_to_end(&mut bytes)
                .map_err(|e| e.to_string())?;
            if bytes.len() > limit {
                return Err("JSON grew past byte limit".into());
            }
            Ok(bytes)
        }
    }
}
fn components(p: &Path) -> Result<Vec<&std::ffi::OsStr>, String> {
    if p.as_os_str().len() > 4096 {
        return Err("path length limit".into());
    }
    let mut parts = Vec::new();
    for c in p.components() {
        match c {
            Component::Normal(n) if !n.to_string_lossy().starts_with('.') => parts.push(n),
            _ => return Err("relative visible components required; no parent traversal".into()),
        }
    }
    if parts.len() > 16 {
        return Err("path component limit".into());
    }
    Ok(parts)
}

#[cfg(unix)]
struct Reader {
    roots: Vec<bounded::Root>,
    out: Collection,
    cache: BTreeMap<(usize, PathBuf), usize>,
}
#[cfg(unix)]
impl Reader {
    fn problem(&mut self, p: impl Into<String>) {
        if self.out.problems.len() < 256 {
            self.out.problems.push(p.into());
        } else {
            self.out.partial = true;
        }
    }
    fn snapshot(&mut self, r: usize, p: &Path) -> Result<Document, String> {
        components(p)?;
        if p.extension().and_then(|v| v.to_str()) != Some("json") {
            return Err("only visible JSON associations allowed".into());
        }
        let remaining = MAX_TOTAL.saturating_sub(self.out.snapshot_bytes);
        if remaining == 0 {
            self.out.partial = true;
            return Err("total snapshot byte limit reached".into());
        }
        let bytes = match self.roots[r].bytes(p, MAX_FILE.min(remaining)) {
            Ok(b) => b,
            Err(e) => {
                if e.contains("limit") {
                    self.out.partial = true;
                }
                return Err(e);
            }
        };
        let size_bytes = bytes.len();
        self.out.snapshot_bytes += bytes.len();
        let sha256 = format!("{:x}", Sha256::digest(&bytes));
        let raw_text = String::from_utf8(bytes).ok();
        let parsed = raw_text
            .as_ref()
            .ok_or_else(|| "invalid UTF-8".to_owned())
            .and_then(|s| {
                serde_json::from_str::<Unique>(s)
                    .map(|u| u.0)
                    .map_err(|e| e.to_string())
            });
        let (json, problem) = match parsed {
            Ok(v) => (Some(v), None),
            Err(e) => (None, Some(e)),
        };
        Ok(Document {
            path: self.roots[r].path.join(p).display().to_string(),
            sha256,
            size_bytes,
            raw_text,
            json,
            problem,
        })
    }
    fn store(&mut self, r: usize, p: &Path, d: Document) -> usize {
        let i = self.out.documents.len();
        self.out.documents.push(d);
        self.cache.insert((r, p.to_owned()), i);
        i
    }
    fn document(&mut self, r: usize, p: &Path) -> Result<usize, String> {
        if let Some(i) = self.cache.get(&(r, p.to_owned())) {
            return Ok(*i);
        }
        let d = self.snapshot(r, p)?;
        Ok(self.store(r, p, d))
    }
    fn association_path(
        &self,
        r: usize,
        parent: &Path,
        raw: &str,
    ) -> Result<(usize, PathBuf), String> {
        let path = Path::new(raw);
        if path.components().any(|c| c == Component::ParentDir) {
            return Err("parent traversal refused".into());
        }
        let (root, relative) = if path.is_absolute() {
            self.roots
                .iter()
                .enumerate()
                .find_map(|(i, root)| {
                    path.strip_prefix(&root.path)
                        .ok()
                        .map(|p| (i, p.to_owned()))
                })
                .ok_or_else(|| "absolute association outside fixed roots".to_owned())?
        } else {
            (r, parent.join(path))
        };
        components(&relative)?;
        Ok((root, relative))
    }
    fn associate(
        &mut self,
        case: &mut Case,
        r: usize,
        parent: &Path,
        raw: &str,
        role: &str,
        expected: Option<&str>,
    ) -> Option<usize> {
        if case.associations.len() >= MAX_ASSOCIATIONS {
            self.out.partial = true;
            case.issues
                .push("association limit reached; remaining references not read".into());
            return None;
        }
        let result = self
            .association_path(r, parent, raw)
            .and_then(|(root, p)| self.document(root, &p));
        match result {
            Ok(i) => {
                let doc = &self.out.documents[i];
                let integrity = match expected {
                    Some(s) if digest_ok(s) && s == doc.sha256 => "sha_match_declaration_only",
                    Some(_) => "sha_mismatch",
                    None => "unbound_context",
                };
                if integrity == "sha_mismatch" {
                    case.issues.push(format!("{role}: snapshot hash mismatch"));
                }
                if let Some(e) = &doc.problem {
                    case.issues.push(format!("{role}: {e}"));
                }
                case.associations.push(Association {
                    document: i,
                    role: role.into(),
                    integrity: integrity.into(),
                });
                Some(i)
            }
            Err(e) => {
                case.issues.push(format!("{role}: {e}"));
                None
            }
        }
    }
    fn context(&mut self, case: &mut Case, r: usize, parent: &Path) {
        for name in ["case_context.json", "summary.json"] {
            if case.associations.len() >= MAX_ASSOCIATIONS {
                self.out.partial = true;
                case.issues
                    .push("context omitted at association limit".into());
                break;
            }
            // Context is optional; only retain present regular JSON snapshots.
            if let Ok(i) = self.document(r, &parent.join(name)) {
                case.associations.push(Association {
                    document: i,
                    role: name.into(),
                    integrity: "unbound_context".into(),
                });
                let doc = &self.out.documents[i];
                if let Some(e) = &doc.problem {
                    case.issues.push(format!("{name}: {e}"));
                }
                if let Some(v) = &doc.json {
                    if name == "case_context.json" && v["schema"] == "game_opt_loop/case_context@1"
                    {
                        if let Some(origin) = text(v, "evidence_origin") {
                            case.source_declarations
                                .push(format!("context:{origin} (unverified)"));
                        }
                        if let Some(scope) = text(v, "scope") {
                            case.issues.push(format!("context: {scope}"));
                        }
                    }
                    if name == "summary.json" {
                        if let Some(scope) = text(v, "scope") {
                            case.issues.push(format!("unverified summary: {scope}"));
                        }
                    }
                }
            }
        }
    }
    fn confirmation(&mut self, case: &mut Case, r: usize, parent: &Path, v: &Value) {
        case.declared_status = text(v, "verdict");
        case.scope = text(v, "claim_scope");
        case.issues.extend(strings(v.get("issues")));
        if let Some(a) = text(v, "source_attestation") {
            case.issues
                .push(format!("source attestation declaration: {a}"));
        }
        if !matches!(
            case.declared_status.as_deref(),
            Some("PASS" | "INCONCLUSIVE" | "BLOCKED")
        ) {
            case.issues
                .push("missing/unknown saved confirmation verdict".into());
        }
        let samples = self.associate(
            case,
            r,
            parent,
            "samples.json",
            "confirmation samples",
            v["samples_sha256"].as_str(),
        );
        let Some(i) = samples else {
            return;
        };
        if !digest_ok(v["samples_sha256"].as_str().unwrap_or(""))
            || self.out.documents[i].sha256 != v["samples_sha256"].as_str().unwrap_or("")
        {
            return;
        }
        let Some(samples) = self.out.documents[i].json.clone() else {
            return;
        };
        if let Some(kind) = text(&samples, "source_kind") {
            case.source_declarations
                .push(format!("samples:{kind} (unverified)"));
        }
        let Some(pairs) = samples["pairs"].as_array() else {
            case.issues.push("samples pairs missing/malformed".into());
            return;
        };
        for pair in pairs.iter().take(MAX_ASSOCIATIONS / 2) {
            for arm in ["a", "b"] {
                let (Some(raw), Some(run), Some(hash)) = (
                    pair[format!("{arm}_report")].as_str(),
                    pair[format!("{arm}_run_id")].as_str(),
                    pair[format!("{arm}_report_sha256")].as_str(),
                ) else {
                    case.issues.push("missing arm reference/hash/run ID".into());
                    continue;
                };
                if v["report_hashes"][run].as_str() != Some(hash) {
                    case.issues.push(format!(
                        "{run}: sample and saved report hash declarations disagree"
                    ));
                    continue;
                }
                if let Some(i) =
                    self.associate(case, r, parent, raw, &format!("receiver {run}"), Some(hash))
                {
                    if let Some(raw) = &self.out.documents[i].json {
                        if let Some(origin) = raw["qualification"]["evidence_origin"].as_str() {
                            case.source_declarations
                                .push(format!("receiver:{origin} (unverified)"));
                        }
                    }
                }
            }
        }
        if pairs.len() > MAX_ASSOCIATIONS / 2 {
            self.out.partial = true;
            case.issues
                .push("pair association limit reached; not all receiver reports loaded".into());
        }
    }
    fn model(&mut self, case: &mut Case, r: usize, parent: &Path, v: &Value) {
        case.title = text(&v["frozen"]["plan"], "pipeline_id");
        case.scope = text(v, "completion_scope");
        let Some(entries) = v["entries"].as_array() else {
            case.issues.push("stage entries missing/malformed".into());
            return;
        };
        let mut names = BTreeSet::new();
        for e in entries.iter().take(5) {
            let name = text(e, "stage");
            let state = text(e, "state");
            if !name.as_ref().is_some_and(|s| names.insert(s.clone())) {
                case.issues.push("missing/duplicate stage identity".into());
            }
            if !matches!(
                state.as_deref(),
                Some(
                    "pending"
                        | "deferred"
                        | "ready"
                        | "rejected"
                        | "in_flight"
                        | "unknown"
                        | "completed"
                        | "failed"
                )
            ) {
                case.issues.push("missing/unknown saved stage state".into());
            }
            case.stages.push(Stage {
                name,
                state,
                error: text(e, "error"),
                decisions: e.get("decisions").filter(|v| !v.is_null()).cloned(),
                reconciliation: e["evidence"]
                    .get("reconciliation")
                    .filter(|v| !v.is_null())
                    .cloned(),
                usage: e["evidence"]["receipt"]
                    .get("usage")
                    .filter(|v| !v.is_null())
                    .cloned(),
            });
            if let Some(raw) = e["evidence"]["receipt_file"].as_str() {
                self.associate(
                    case,
                    r,
                    parent,
                    raw,
                    "stage receipt",
                    e["evidence"]["receipt_sha256"].as_str(),
                );
            }
            if let Some(outputs) = e["evidence"]["outputs"].as_array() {
                for out in outputs.iter().take(MAX_ASSOCIATIONS) {
                    if let Some(source) = text(out, "source") {
                        case.source_declarations
                            .push(format!("output:{source} (unverified)"));
                    }
                    if matches!(
                        out["role"].as_str(),
                        Some("evaluation_report" | "deployment_receipt")
                    ) {
                        if let Some(path) = out["path"].as_str() {
                            self.associate(
                                case,
                                r,
                                parent,
                                path,
                                "model output",
                                out["sha256"].as_str(),
                            );
                        }
                    }
                }
                if outputs.len() > MAX_ASSOCIATIONS {
                    self.out.partial = true;
                    case.issues.push("output reference limit reached".into());
                }
            }
        }
        let expected: BTreeSet<String> = ["design", "train", "quantize", "deploy", "evaluate"]
            .into_iter()
            .map(str::to_owned)
            .collect();
        if entries.len() != 5 || names != expected {
            case.issues
                .push("five distinct known stages not present".into());
        }
        if entries.len() > 5 {
            self.out.partial = true;
            case.issues
                .push("stage summary limited to five entries; full journal retained".into());
        }
        case.declared_status = if entries.len() == 5
            && names == expected
            && case
                .stages
                .iter()
                .all(|e| e.state.as_deref() == Some("completed"))
        {
            Some("completed_protocol_declaration".into())
        } else {
            Some("saved_stage_states_only".into())
        };
        if let Some(data) = v["frozen"]["plan"]["data"].as_array() {
            for d in data.iter().take(32) {
                if let Some(s) = text(d, "source") {
                    case.source_declarations
                        .push(format!("data:{s} (unverified)"));
                }
            }
        }
        case.issues.push("No driver is executed; UNKNOWN/in_flight cannot be resumed or reconciled in this view.".into());
        case.issues.push(format!(
            "usage verification declaration: {}",
            text(v, "usage_verification").unwrap_or_else(|| "unknown".into())
        ));
    }
    fn primary(&mut self, r: usize, p: &Path, d: Document) {
        let kind = match d.json.as_ref().and_then(|v| v["schema"].as_str()) {
            Some(CONFIRM) => "confirmation",
            Some(MODEL) => "model_pipeline",
            _ => "unknown",
        };
        let i = match self.cache.get(&(r, p.to_owned())) {
            Some(i) => *i,
            None => self.store(r, p, d),
        };
        let v = self.out.documents[i].json.clone();
        let mut case = Case {
            id: self.out.documents[i].path.clone(),
            kind,
            title: Some(p.display().to_string()),
            declared_status: None,
            source_declarations: Vec::new(),
            qualification: v.as_ref().and_then(|v| v.get("qualification")).cloned(),
            scope: None,
            issues: Vec::new(),
            stages: Vec::new(),
            primary_document: i,
            associations: Vec::new(),
            view_grants_game_proof: false,
            view_allows_promotion: false,
        };
        if let Some(e) = &self.out.documents[i].problem {
            case.issues.push(format!("primary parse: {e}"));
        }
        if self
            .out
            .items
            .iter()
            .any(|c| self.out.documents[c.primary_document].sha256 == self.out.documents[i].sha256)
        {
            case.issues
                .push("duplicate primary byte snapshot; not an independent experiment".into());
        }
        let parent = p.parent().unwrap_or(Path::new(""));
        if let Some(v) = v {
            match kind {
                "confirmation" => self.confirmation(&mut case, r, parent, &v),
                "model_pipeline" => self.model(&mut case, r, parent, &v),
                _ => case.issues.push(
                    "unknown/malformed primary schema; no status or qualification inferred".into(),
                ),
            }
            for key in [
                "promotion_allowed",
                "real_game_optimization_proven",
                "model_semantics_verified",
            ] {
                if v.get(key).is_some_and(|x| x != &Value::Bool(false)) {
                    case.issues.push(format!(
                        "{key}: contradictory/unknown primary declaration, never a view permission"
                    ));
                }
            }
        }
        self.context(&mut case, r, parent);
        case.source_declarations.sort();
        case.source_declarations.dedup();
        self.out.items.push(case);
    }
    fn scan(&mut self) {
        let mut queue = VecDeque::new();
        for r in 0..self.roots.len() {
            for p in ["runs", "tasks"] {
                queue.push_back((r, PathBuf::from(p), 0));
            }
        }
        while let Some((r, p, depth)) = queue.pop_front() {
            if self.out.scanned_directories >= MAX_DIRS
                || self.out.scanned_entries >= MAX_ENTRIES
                || self.out.items.len() >= MAX_CASES
                || self.out.snapshot_bytes >= MAX_TOTAL
            {
                self.out.partial = true;
                self.problem("discovery/case/snapshot limit reached; list is partial");
                break;
            }
            let (names, limited) =
                match self.roots[r].names(&p, MAX_ENTRIES - self.out.scanned_entries) {
                    Ok(v) => v,
                    Err(e) => {
                        self.out.partial = true;
                        self.problem(format!(
                            "{}: directory unavailable: {e}",
                            self.roots[r].path.join(&p).display()
                        ));
                        continue;
                    }
                };
            self.out.scanned_directories += 1;
            self.out.scanned_entries += names.len();
            if limited {
                self.out.partial = true;
                self.problem("directory entry limit reached; list is partial");
            }
            for name in names {
                if name.to_string_lossy().starts_with('.') {
                    continue;
                }
                let child = p.join(name);
                if self.roots[r].is_dir(&child) {
                    if depth < MAX_DEPTH {
                        queue.push_back((r, child, depth + 1));
                    } else {
                        self.out.partial = true;
                        self.problem(format!("depth limit: {}", child.display()));
                    }
                    continue;
                }
                if child.extension().and_then(|v| v.to_str()) != Some("json") {
                    continue;
                }
                if self.out.items.len() >= MAX_CASES || self.out.snapshot_bytes >= MAX_TOTAL {
                    self.out.partial = true;
                    break;
                }
                if let Some(i) = self.cache.get(&(r, child.clone())).copied() {
                    let doc = &self.out.documents[i];
                    let schema = doc.json.as_ref().and_then(|v| v["schema"].as_str());
                    if matches!(schema, Some(CONFIRM | MODEL))
                        && !self.out.items.iter().any(|c| c.id == doc.path)
                    {
                        self.primary(r, &child, doc.clone());
                    }
                    continue;
                }
                match self.snapshot(r, &child) {
                    Ok(d) => {
                        let schema = d.json.as_ref().and_then(|v| v["schema"].as_str());
                        let hint = matches!(
                            child.file_name().and_then(|v| v.to_str()),
                            Some("pipeline.json" | "judge.json")
                        );
                        if matches!(schema, Some(CONFIRM | MODEL)) || hint {
                            self.primary(r, &child, d);
                        }
                    }
                    Err(e) => self.problem(format!("{}: {e}", child.display())),
                }
            }
        }
        if self.out.partial {
            self.problem(
                "partial discovery/references: do not interpret these counts as all experiments.",
            );
        }
    }
}

pub fn collect(gol: &Path, phonefarm: &Path) -> Collection {
    let out = Collection {
        items: Vec::new(),
        documents: Vec::new(),
        problems: Vec::new(),
        partial: false,
        scanned_directories: 0,
        scanned_entries: 0,
        snapshot_bytes: 0,
        view_grants_game_proof: false,
        view_allows_promotion: false,
    };
    #[cfg(unix)]
    {
        let mut reader = Reader {
            roots: Vec::new(),
            out,
            cache: BTreeMap::new(),
        };
        for path in [gol, phonefarm] {
            match bounded::Root::open(path) {
                Ok(root) => {
                    if !reader.roots.iter().any(|r| r.path == root.path) {
                        reader.roots.push(root);
                    }
                }
                Err(e) => {
                    reader.out.partial = true;
                    reader.problem(format!("{}: root unavailable: {e}", path.display()));
                }
            }
        }
        reader.scan();
        reader.out
    }
    #[cfg(not(unix))]
    {
        let _ = (gol, phonefarm);
        let mut out = out;
        out.partial = true;
        out.problems
            .push("NOT_RUN: descriptor-based bounded case snapshots require Unix".into());
        out
    }
}
