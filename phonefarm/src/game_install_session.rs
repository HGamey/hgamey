//! Fresh single-APK installation. Committed packages are intentionally retained.
//!
//! On a rooted phone the package manager commands run as root: ColorOS sends a shell install to its own approval
//! screens (a risk page, then a slider puzzle), and a root install skips them. A commit the phone refuses for policy,
//! or one still waiting after a while, is abandoned, and a phone with Magisk then gets the APK as a systemless system
//! app instead: a module under /data/adb/modules, mounted by a reboot. Removing that module and rebooting uninstalls it.
use crate::{
    device_registry::DeviceBinding,
    devlease::Lease,
    game_deploy::{self, InstallRequest, INSTALL_SCOPE},
    game_deploy_session::{digest, package_identity, q, valid_boot, Remote},
    game_deploy_transport::{ShellStderr, Transport, TransportError},
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs::File,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

pub(crate) const SCHEMA: &str = "phonefarm/game-install-session@1";
static INTERRUPTED: AtomicBool = AtomicBool::new(false);
extern "C" fn interrupt(_: libc::c_int) {
    INTERRUPTED.store(true, Ordering::SeqCst);
}
fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}
fn stage(token: &str) -> String {
    format!("/data/local/tmp/pf-install-{token}")
}
const MODULES: &str = "/data/adb/modules";
const BUSYBOX: &str = "/data/adb/magisk/busybox";
/// Package manager failures that are about the APK or the phone, not its install policy: a module would not help.
const GENUINE_FAILURES: [&str; 13] = [
    "INSUFFICIENT_STORAGE",
    "NO_MATCHING_ABIS",
    "CPU_ABI_INCOMPATIBLE",
    "ALREADY_EXISTS",
    "DUPLICATE_PACKAGE",
    "INVALID_APK",
    "UPDATE_INCOMPATIBLE",
    "VERSION_DOWNGRADE",
    "OLDER_SDK",
    "NEWER_SDK",
    "DEPRECATED_SDK_VERSION",
    "MISSING_SHARED_LIBRARY",
    "PARSE_FAILED",
];
/// Install screens a waiting commit leaves open, closed once it is abandoned (they may sit under other dialogs).
const INSTALLER_SCREENS: [&str; 3] = [
    "com.oplus.appdetail",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
];
fn millis(name: &str, default: u64) -> Duration {
    Duration::from_millis(
        std::env::var(name)
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(default),
    )
}
fn module_id(package: &str) -> String {
    format!("pf_game_{}", package.replace('.', "_"))
}
fn abi_dir(abi: &str) -> Option<&'static str> {
    Some(match abi {
        "arm64-v8a" => "arm64",
        "armeabi-v7a" | "armeabi" => "arm",
        "x86_64" => "x86_64",
        "x86" => "x86",
        _ => return None,
    })
}

/// A systemless install: the module this session wrote, and the boot that mounted it.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Module {
    id: String,
    written: bool,
    mounted_boot: Option<String>,
    verified: bool,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    schema: String,
    request: InstallRequest,
    token: String,
    boot_id: Option<String>,
    phase: String,
    stage_attempted: bool,
    write_pending: Option<String>,
    transport_uncertain: bool,
    create_attempted: bool,
    session_id: Option<u32>,
    session_abandoned: bool,
    commit_attempted: bool,
    commit_confirmed: bool,
    checks: Value,
    source_session: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    device_binding: Option<DeviceBinding>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    lease_scope: Option<String>,
    /// Package manager commands run as root (`su -c id -u` printed 0).
    #[serde(default)]
    root: bool,
    /// Root can write Magisk modules.
    #[serde(default)]
    magisk: bool,
    /// The commit ended without installing: the package manager's refusal, or the wait that was abandoned.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    commit_outcome: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    module: Option<Module>,
}
impl State {
    fn validate(&self) -> Result<(), String> {
        self.request.validate()?;
        game_deploy::validate_scope(
            self.device_binding.as_ref(),
            self.lease_scope.as_deref(),
            &self.request.serial,
        )?;
        if self.schema != SCHEMA
            || self.token.is_empty()
            || self.token.len() > 128
            || !self
                .token
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            || !matches!(
                self.phase.as_str(),
                "prepared" | "checked" | "applying" | "committed" | "settled"
            )
            || !self.checks.is_object()
            || self.boot_id.as_ref().is_some_and(|v| !valid_boot(v))
            || self
                .session_id
                .is_some_and(|id| id == 0 || id > i32::MAX as u32)
            || (self.session_id.is_some() && !self.create_attempted)
            || (self.create_attempted && !self.stage_attempted)
            || (self.commit_attempted && self.session_id.is_none())
            || (self.commit_attempted && self.session_abandoned && self.commit_outcome.is_none())
            || (self.commit_confirmed && (!self.commit_attempted || self.commit_outcome.is_some()))
            || (self.phase == "committed" && !self.commit_confirmed && !self.module_verified())
            || (self.module.is_some() && (!self.magisk || self.commit_outcome.is_none()))
            || (self.magisk && !self.root)
            || (matches!(self.phase.as_str(), "prepared" | "checked") && self.stage_attempted)
            || (self.session_abandoned && self.session_id.is_none())
            || (self.stage_attempted && self.boot_id.is_none())
        {
            return Err("invalid installation session identity or state".into());
        }
        Ok(())
    }
    fn module_verified(&self) -> bool {
        self.module.as_ref().is_some_and(|m| m.verified)
    }
}
pub(crate) struct Session {
    lease: Lease,
    state: State,
    dir: PathBuf,
    out: PathBuf,
    transport: Transport,
    // Only this live creator may abandon a non-committed session. A recovered
    // numeric session ID alone is not proof of its present service-side owner.
    created_here: bool,
}
impl Session {
    pub(crate) fn new(lease: Lease, request: InstallRequest, out: &Path) -> Result<Self, String> {
        let token = lease.token().to_owned();
        let binding = lease.device_binding().cloned();
        Self::from_state(
            lease,
            State {
                schema: SCHEMA.into(),
                request,
                token,
                boot_id: None,
                phase: "prepared".into(),
                stage_attempted: false,
                write_pending: None,
                transport_uncertain: false,
                create_attempted: false,
                session_id: None,
                session_abandoned: false,
                commit_attempted: false,
                commit_confirmed: false,
                checks: json!({}),
                source_session: None,
                lease_scope: binding.as_ref().map(|_| "device".into()),
                device_binding: binding,
                root: false,
                magisk: false,
                commit_outcome: None,
                module: None,
            },
            out,
        )
    }
    fn from_state(lease: Lease, state: State, out: &Path) -> Result<Self, String> {
        state.validate()?;
        if lease.device_binding() != state.device_binding.as_ref() {
            return Err("lease device scope differs from saved transaction".into());
        }
        let dir = out.join("game-deploy");
        let transport = Transport::new(
            &state.request.serial,
            &state.token,
            &dir,
            ShellStderr::Merged,
        )?;
        std::fs::create_dir(&dir).map_err(|e| e.to_string())?;
        File::open(&dir)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())?;
        File::open(out)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())?;
        let session = Self {
            lease,
            state,
            dir,
            out: out.to_owned(),
            transport,
            created_here: false,
        };
        session.save()?;
        Ok(session)
    }
    fn save(&self) -> Result<(), String> {
        crate::devlease::durable_replace(
            &self.dir.join("session.json"),
            &serde_json::to_vec_pretty(&self.state).map_err(|e| e.to_string())?,
        )
    }
    fn uncertain(&mut self, error: &str) -> String {
        self.state.transport_uncertain = true;
        let hold = self.lease.hold_for_recovery(&self.dir).err();
        let save = self.save().err();
        format!(
            "{error}{}{}",
            hold.map(|e| format!("; retain: {e}")).unwrap_or_default(),
            save.map(|e| format!("; state: {e}")).unwrap_or_default()
        )
    }
    fn transport_ready(&self) -> Result<(), String> {
        if self.state.transport_uncertain {
            return Err("installation transport uncertain; further device commands refused".into());
        }
        Ok(())
    }
    fn transport_error(&mut self, error: TransportError) -> String {
        if error.uncertain {
            self.uncertain(&error.message)
        } else {
            error.message
        }
    }
    fn raw(
        &mut self,
        operation: &str,
        args: Vec<String>,
        seconds: u64,
    ) -> Result<crate::lease_command::Output, String> {
        self.transport_ready()?;
        self.transport
            .raw(&self.lease, operation, args, seconds)
            .map_err(|error| self.transport_error(error))
    }
    fn shell(&mut self, operation: &str, command: &str, seconds: u64) -> Result<Remote, String> {
        self.transport_ready()?;
        self.transport
            .shell(&self.lease, operation, command, seconds)
            .map_err(|error| self.transport_error(error))
    }
    fn read(&mut self, operation: &str, command: &str) -> Result<String, String> {
        let output = self.shell(operation, command, 30)?;
        if output.code != 0 {
            return Err(format!(
                "{operation} failed on device (exit {})",
                output.code
            ));
        }
        Ok(output.stdout.trim().to_owned())
    }
    fn intent(&mut self, operation: &str) -> Result<(), String> {
        self.lease.hold_for_recovery(&self.dir)?;
        self.state.write_pending = Some(operation.into());
        self.save()
    }
    fn clear_pending(&mut self) -> Result<(), String> {
        let pending = self.state.write_pending.take();
        if let Err(e) = self.save() {
            self.state.write_pending = pending;
            return Err(e);
        }
        Ok(())
    }
    fn write(&mut self, operation: &str, command: &str, seconds: u64) -> Result<Remote, String> {
        self.intent(operation)?;
        let output = self.shell(operation, command, seconds)?;
        self.clear_pending()?;
        Ok(output)
    }
    fn checked_write(&mut self, operation: &str, command: &str) -> Result<(), String> {
        let output = self.write(operation, command, 30)?;
        if output.code != 0 {
            return Err(format!(
                "{operation} failed on device (exit {})",
                output.code
            ));
        }
        Ok(())
    }
    fn absence(&mut self) -> Result<(), String> {
        let packages = self.read("package_absence", "pm list packages -u --user all")?;
        if let Err(error) = require_absent(&packages, &self.state.request.recipe.package) {
            let id = module_id(&self.state.request.recipe.package);
            let dir = q(&format!("{MODULES}/{id}"));
            if self.state.root && self.shell("module_presence", &su(&format!("test -d {dir}")), 30)?.code == 0 {
                return Err(format!("{error}; it was installed by the Magisk module {id}: \
                    uninstall it with `su -c 'touch {MODULES}/{id}/remove'` and a reboot"));
            }
            return Err(error);
        }
        self.state.checks["package_absence"] = json!({"status":"verified","including_retained":true,"users":"all","observed_at":now()});
        self.save()
    }
    fn preflight(&mut self) -> Result<(), String> {
        let metadata = crate::apk_metadata::inspect(
            &self.lease,
            &self.dir.join("apk-metadata"),
            &self.state.request.assets[0],
        )?;
        if metadata["package"] != self.state.request.recipe.package
            || metadata["version_code"].as_u64() != Some(self.state.request.recipe.version_code)
            || metadata["single_base_apk"] != true
            || metadata["shared_uid"] != false
            || metadata["test_only"] != false
        {
            return Err("APK metadata does not match the declared fresh single-APK recipe".into());
        }
        self.state.checks["apk_metadata"] = metadata.clone();
        self.save()?;
        if self.read("current_user", "am get-current-user")? != "0" {
            return Err("only Android user 0 is supported".into());
        }
        let boot = self.read("boot_id", "cat /proc/sys/kernel/random/boot_id")?;
        if !valid_boot(&boot) {
            return Err("device boot identity unavailable".into());
        }
        self.state.boot_id = Some(boot.clone());
        let sdk = self
            .read("device_sdk", "getprop ro.build.version.sdk")?
            .parse::<u64>()
            .map_err(|_| "device SDK unavailable")?;
        if sdk
            < metadata["min_sdk"]
                .as_u64()
                .ok_or("APK minSDK unavailable")?
        {
            return Err("APK minSDK exceeds device SDK".into());
        }
        let abi_text = self.read("device_abis", "getprop ro.product.cpu.abilist")?;
        let abis: Vec<&str> = abi_text.split(',').collect();
        if abis.iter().any(|v| {
            v.is_empty()
                || !v
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        }) {
            return Err("device ABI list unavailable".into());
        }
        let apk_abis = metadata["abis"]
            .as_array()
            .ok_or("APK ABI metadata unavailable")?;
        if !apk_abis.is_empty()
            && !apk_abis
                .iter()
                .any(|v| v.as_str().is_some_and(|s| abis.contains(&s)))
        {
            return Err("APK native ABI does not match device".into());
        }
        let help = self.read("package_manager_capabilities", "pm help")?;
        require_installer(&help)?;
        // su can exit 0 without root (Magisk's daemon not up): only the printed uid counts.
        let uid = self.shell("root_identity", "su -c 'id -u' 2>/dev/null", 30)?;
        self.state.root = uid.code == 0 && uid.stdout.trim() == "0";
        let magisk = if self.state.root {
            let check = self.shell(
                "magisk_modules",
                &su(&format!("magisk -V && test -d {MODULES} && test -x {BUSYBOX}")),
                30,
            )?;
            let version = check.stdout.trim().to_owned();
            (check.code == 0 && !version.is_empty() && version.bytes().all(|b| b.is_ascii_digit()))
                .then_some(version)
        } else {
            None
        };
        self.state.magisk = magisk.is_some();
        self.state.checks["device"] = json!({"user":0,"boot_id":boot,"sdk":sdk,"abis":abis});
        self.state.checks["installer"] = json!({"no_replace_flag":"-R","explicit_session_commands":true,
            "root":self.state.root,"magisk_version":magisk});
        self.absence()?;
        self.read("install_stage_parent","test -d /data/local/tmp && test ! -L /data/local/tmp && test -r /data/local/tmp && test -x /data/local/tmp && test -w /data/local/tmp")?;
        self.read(
            "install_stage_absent",
            &format!(
                "test ! -e {0} && test ! -L {0}",
                q(&stage(&self.state.token))
            ),
        )?;
        self.save()
    }
    fn boot(&mut self) -> Result<(), String> {
        let current = self.read("recovery_boot", "cat /proc/sys/kernel/random/boot_id")?;
        if self.state.boot_id.as_deref() != Some(current.as_str()) {
            return Err("device rebooted; installation reconciliation requires review".into());
        }
        Ok(())
    }
    fn owner(&mut self) -> Result<(), String> {
        let root = q(&stage(&self.state.token));
        let marker = q(&format!("{}/.phonefarm-owner", stage(&self.state.token)));
        self.read("install_stage_owner",&format!("test -d {root} && test ! -L {root} && test -f {marker} && test ! -L {marker} && test \"$(cat {marker})\" = {}",q(&self.state.token)))?;
        Ok(())
    }
    fn apply(&mut self) -> Result<(), String> {
        if INTERRUPTED.load(Ordering::SeqCst) {
            return Err("installation interrupted before first write".into());
        }
        self.state.phase = "applying".into();
        self.state.stage_attempted = true;
        self.lease.hold_for_recovery(&self.dir)?;
        self.save()?;
        let root = stage(&self.state.token);
        self.checked_write(
            "create_install_stage",
            &format!(
                "mkdir {} && (set -C; printf '%s\\n' {} > {})",
                q(&root),
                q(&self.state.token),
                q(&format!("{root}/.phonefarm-owner"))
            ),
        )?;
        self.owner()?;
        let asset = self.state.request.assets[0].clone();
        let source = game_deploy::source_file(&asset)?;
        let before = source.metadata().map_err(|e| e.to_string())?;
        let temp = format!("{root}/base.apk.part");
        let apk = format!("{root}/base.apk");
        self.intent("push_apk")?;
        let pushed = self.raw(
            "push_apk",
            vec![
                "push".into(),
                asset.path.to_string_lossy().into_owned(),
                temp.clone(),
            ],
            1800,
        )?;
        if pushed.code != 0 {
            return Err(self.uncertain("ADB push did not confirm APK completion"));
        }
        self.clear_pending()?;
        let after = std::fs::symlink_metadata(&asset.path).map_err(|e| e.to_string())?;
        if !after.is_file()
            || before.dev() != after.dev()
            || before.ino() != after.ino()
            || before.len() != after.len()
            || before.mtime() != after.mtime()
            || before.mtime_nsec() != after.mtime_nsec()
        {
            return Err("APK cache source changed while pushing".into());
        }
        let size = self.read(
            "apk_receiver_size",
            &format!("test ! -L {0} && test -f {0} && wc -c < {0}", q(&temp)),
        )?;
        if size.parse::<u64>().ok() != Some(asset.size) {
            return Err("received APK size mismatch".into());
        }
        let hash = self.read("apk_receiver_sha256", &format!("sha256sum {}", q(&temp)))?;
        if digest(&hash)? != asset.sha256 {
            return Err("received APK SHA-256 mismatch".into());
        }
        self.checked_write(
            "publish_apk",
            &format!(
                "test ! -e {0} && test ! -L {0} && mv {1} {0}",
                q(&apk),
                q(&temp)
            ),
        )?;
        if INTERRUPTED.load(Ordering::SeqCst) {
            return Err("installation interrupted before PackageInstaller session".into());
        }
        self.state.create_attempted = true;
        self.save()?;
        let create = self.pm(&format!(
            "pm install-create -R --user 0 --pkg {} -S {}",
            q(&self.state.request.recipe.package),
            asset.size
        ));
        let created = self.write("install_create", &create, 60)?;
        if created.code != 0 {
            return Err(
                "PackageInstaller create result does not establish an owned session".into(),
            );
        }
        let id = parse_session_id(&created.stdout)?;
        self.state.session_id = Some(id);
        self.created_here = true;
        self.save()?;
        if INTERRUPTED.load(Ordering::SeqCst) {
            return Err("installation interrupted after session creation".into());
        }
        let write = self.pm(&format!(
            "pm install-write -S {} {id} base.apk {}",
            asset.size,
            q(&apk)
        ));
        let written = self.write("install_write", &write, 1800)?;
        if written.code != 0
            || written.stdout.trim() != format!("Success: streamed {} bytes", asset.size)
        {
            return Err("PackageInstaller did not confirm all APK bytes".into());
        }
        if INTERRUPTED.load(Ordering::SeqCst) {
            return Err("installation interrupted before commit".into());
        }
        self.absence()?;
        self.state.commit_attempted = true;
        self.save()?;
        match self.commit(id)? {
            None => {
                self.state.commit_confirmed = true;
                self.state.phase = "committed".into();
                self.save()?;
                self.verify_installed()?;
            }
            Some(outcome) => {
                if let Some(reason) = GENUINE_FAILURES.iter().find(|r| outcome.contains(*r)) {
                    return Err(format!("PackageInstaller refused the APK ({reason}): {outcome}"));
                }
                if !self.state.magisk {
                    return Err(format!("PackageInstaller did not install the APK and this phone has no root Magisk to install it as a module: {outcome}"));
                }
                self.absence()?;
                self.install_module()?;
            }
        }
        self.cleanup_stage()?;
        self.state.phase = "settled".into();
        self.save()
    }
    fn pm(&self, command: &str) -> String {
        if self.state.root {
            su(command)
        } else {
            command.into()
        }
    }
    /// Commits in the background on the phone and waits for its answer. None: installed. Some: why not, with the
    /// session gone (refused, or abandoned after waiting), so the package cannot appear later from this commit.
    fn commit(&mut self, id: u32) -> Result<Option<String>, String> {
        let root = stage(&self.state.token);
        let (result, part) = (format!("{root}/commit.result"), format!("{root}/commit.result.part"));
        let run = format!(
            "{} > {part} 2>&1; echo PF_COMMIT_EXIT:$? >> {part}; mv {part} {result}",
            self.pm(&format!("pm install-commit {id}")),
            part = q(&part),
            result = q(&result)
        );
        self.checked_write(
            "install_commit",
            &format!("nohup sh -c {} >/dev/null 2>&1 &", q(&run)),
        )?;
        let wait = millis("PHONEFARM_INSTALL_COMMIT_WAIT_MS", 120_000);
        if let Some(answer) = self.commit_answer(&result, wait)? {
            return Ok(match parse_commit(&answer)? {
                (0, body) if body == "Success" => None,
                (code, body) => {
                    let outcome = format!("PackageInstaller refused (exit {code}): {body}");
                    self.state.commit_outcome = Some(outcome.clone());
                    self.save()?;
                    Some(outcome)
                }
            });
        }
        // Still waiting: ColorOS shows a risk page and a slider puzzle to a shell install, and its cloud check waits
        // forever offline. Abandoning the session ends the commit; its screens are closed so none can finish it.
        let abandoned = self.write("install_abandon", &self.pm(&format!("pm install-abandon {id}")), 60)?;
        if abandoned.code != 0 || abandoned.stdout.trim() != "Success" {
            // The commit may have finished just now, taking its session with it.
            if let Some(answer) = self.commit_answer(&result, Duration::from_secs(10))? {
                if let (0, body) = parse_commit(&answer)? {
                    if body == "Success" {
                        return Ok(None);
                    }
                }
            }
            return Err("PackageInstaller commit is still waiting and did not confirm abandonment".into());
        }
        self.state.session_abandoned = true;
        let outcome = format!("no answer within {} s; the waiting commit was abandoned", wait.as_secs());
        self.state.commit_outcome = Some(outcome.clone());
        self.save()?;
        let answer = self.commit_answer(&result, Duration::from_secs(30))?;
        let close: Vec<_> = INSTALLER_SCREENS.iter().map(|p| format!("am force-stop {} || true", q(p))).collect();
        self.checked_write("close_install_screens", &close.join("; "))?;
        Ok(Some(match answer {
            Some(answer) => format!("{outcome}; it ended with: {}", parse_commit(&answer)?.1),
            None => outcome,
        }))
    }
    fn commit_answer(&mut self, result: &str, wait: Duration) -> Result<Option<String>, String> {
        let deadline = Instant::now() + wait;
        loop {
            let answer = self.read(
                "install_commit_result",
                &format!("if test -f {0}; then cat {0}; fi", q(result)),
            )?;
            if !answer.is_empty() {
                return Ok(Some(answer));
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
            std::thread::sleep(Duration::from_secs(2).min(wait));
        }
    }
    fn verify_installed(&mut self) -> Result<(), String> {
        let module = self.state.module.clone().filter(|m| m.mounted_boot.is_some());
        if !self.state.commit_confirmed && module.is_none() {
            return Err("no confirmed PackageInstaller commit or mounted module".into());
        }
        self.boot()?;
        let package = self.state.request.recipe.package.clone();
        let paths = self.read(
            "installed_apk_path",
            &format!("pm path --user 0 {}", q(&package)),
        )?;
        let lines: Vec<_> = paths.lines().collect();
        let apk = lines
            .first()
            .and_then(|v| v.strip_prefix("package:"))
            .filter(|p| {
                p.starts_with('/') && p.ends_with("/base.apk") && !p.chars().any(char::is_control)
            });
        if lines.len() != 1 || apk.is_none() {
            return Err("installed result is not one base APK".into());
        }
        let apk = apk.unwrap().to_owned();
        if let Some(module) = &module {
            if apk != format!("/system/app/{}/base.apk", module.id) {
                return Err(format!("package is installed from {apk}, not from the Magisk module {}", module.id));
            }
        }
        let info = self.read(
            "installed_package_identity",
            &format!("dumpsys package {}", q(&package)),
        )?;
        let (version, uid) = package_identity(&info)?;
        if version != self.state.request.recipe.version_code {
            return Err("installed versionCode differs from committed request".into());
        }
        let sha = self.read("installed_apk_sha256", &format!("sha256sum {}", q(&apk)))?;
        if digest(&sha)? != self.state.request.recipe.apk_sha256 {
            return Err("installed APK SHA-256 differs from committed request".into());
        }
        self.state.checks["installed"] = json!({"status":"verified","package":package,"version_code":version,"uid":uid,"apk_path":apk,
            "apk_sha256":self.state.request.recipe.apk_sha256,"observed_at":now(),"signature_verification":"package_installer_at_commit","independent_certificate_audit":"not_performed"});
        self.state.checks["installed"]["method"] = json!(match &module {
            Some(_) => "magisk_module",
            None if self.state.root => "package_installer_root",
            None => "package_installer",
        });
        if let Some(module) = &module {
            self.state.checks["installed"]["signature_verification"] = json!("package_manager_at_boot_scan");
            self.state.checks["installed"]["module"] = json!({"id":module.id,"path":format!("{MODULES}/{}", module.id),
                "uninstall":format!("su -c 'touch {MODULES}/{}/remove', then reboot", module.id)});
        }
        self.save()
    }
    /// The APK as a systemless system app: a Magisk module holding system/app/<id>/base.apk and its native libraries,
    /// mounted by a reboot.
    fn install_module(&mut self) -> Result<(), String> {
        let id = module_id(&self.state.request.recipe.package);
        let dir = format!("{MODULES}/{id}");
        let app = format!("{dir}/system/app/{id}");
        if self.shell("module_absent", &su(&format!("test ! -e {0} && test ! -e /data/adb/modules_update/{1}",
                q(&dir), q(&id))), 30)?.code != 0 {
            return Err(format!("a Magisk module {id} already exists; remove it and reboot before installing"));
        }
        self.state.module = Some(Module { id: id.clone(), written: false, mounted_boot: None, verified: false });
        self.save()?;
        let apk = format!("{}/base.apk", stage(&self.state.token));
        let device: Vec<String> = serde_json::from_value(self.state.checks["device"]["abis"].clone())
            .map_err(|_| "device ABI list unavailable")?;
        let packaged = &self.state.checks["apk_metadata"]["abis"];
        let abi = device.iter().find(|abi| packaged.as_array().is_some_and(|list| list.contains(&json!(abi))));
        let libs = match abi {
            Some(abi) => {
                let arch = abi_dir(abi).ok_or("the APK's native ABI has no system library directory")?;
                let temp = q(&format!("{dir}/.lib"));
                format!(" && mkdir {temp} && {BUSYBOX} unzip -q -o {} {} -d {temp} && mkdir -p {} && mv {temp}/lib/{abi}/* {} && rm -r {temp}",
                    q(&apk), q(&format!("lib/{abi}/*")), q(&format!("{app}/lib/{arch}")), q(&format!("{app}/lib/{arch}/")))
            }
            None => String::new(),
        };
        let recipe = &self.state.request.recipe;
        let prop = format!("id={id}\nname={} {}\nversion={}\nversionCode={}\nauthor=phonefarm\ndescription=Installed by phonefarm game-deploy; remove this module and reboot to uninstall\n",
            self.state.request.game_id, self.state.request.version, recipe.version_code, recipe.version_code);
        let script = format!(
            "mkdir {d} && printf '%s\\n' {token} > {d}/.phonefarm-owner && mkdir -p {a} && cp {apk} {a}/base.apk{libs} \
             && printf %s {prop} > {d}/module.prop && chown -R 0:0 {d} && find {d} -type d -exec chmod 755 {{}} + \
             && find {d} -type f -exec chmod 644 {{}} + && chcon -R u:object_r:system_file:s0 {d}/system",
            d = q(&dir), a = q(&app), apk = q(&apk), token = q(&self.state.token), prop = q(&prop));
        let written = self.write("write_magisk_module", &su(&script), 600)?;
        if written.code != 0 {
            return Err(format!("writing the Magisk module failed on device (exit {})", written.code));
        }
        let sha = self.read("module_apk_sha256", &su(&format!("sha256sum {}", q(&format!("{app}/base.apk")))))?;
        if digest(&sha)? != self.state.request.recipe.apk_sha256 {
            return Err("Magisk module APK SHA-256 differs from the request".into());
        }
        self.module_mut()?.written = true;
        self.save()?;
        let boot = self.reboot("module_reboot")?;
        self.module_mut()?.mounted_boot = Some(boot);
        self.save()?;
        self.await_package()?;
        self.verify_installed()?;
        self.module_mut()?.verified = true;
        self.state.phase = "committed".into();
        self.save()
    }
    fn module_mut(&mut self) -> Result<&mut Module, String> {
        self.state.module.as_mut().ok_or_else(|| "no Magisk module in this installation".into())
    }
    /// Reboots and waits for the next boot to complete; the session continues on that boot.
    fn reboot(&mut self, operation: &str) -> Result<String, String> {
        let before = self.state.boot_id.clone().ok_or("device boot identity unavailable")?;
        self.intent(operation)?;
        let output = self.raw(operation, vec!["reboot".into()], 60)?;
        if output.code != 0 {
            return Err(self.uncertain("adb reboot did not confirm"));
        }
        self.clear_pending()?;
        let boot = self.next_boot(&before, millis("PHONEFARM_INSTALL_BOOT_WAIT_MS", 300_000))?
            .ok_or("the phone did not finish booting after the reboot")?;
        self.state.boot_id = Some(boot.clone());
        self.save()?;
        Ok(boot)
    }
    /// The current boot once it has completed; None while the phone is away or still booting. The phone is away
    /// while it reboots, so a failed probe says nothing about the transaction.
    fn probe_boot(&mut self) -> Option<String> {
        let output = self.transport.raw(&self.lease, "boot_probe",
            vec!["shell".into(), "getprop sys.boot_completed; cat /proc/sys/kernel/random/boot_id".into()], 15).ok()?;
        let lines: Vec<_> = output.stdout.lines().map(str::trim).collect();
        (output.code == 0 && lines.len() == 2 && lines[0] == "1" && valid_boot(lines[1])).then(|| lines[1].to_owned())
    }
    /// A completed boot other than `before`, waiting up to `wait`.
    fn next_boot(&mut self, before: &str, wait: Duration) -> Result<Option<String>, String> {
        let deadline = Instant::now() + wait;
        loop {
            if let Some(boot) = self.probe_boot().filter(|boot| boot != before) {
                return Ok(Some(boot));
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
            std::thread::sleep(Duration::from_secs(3).min(wait));
        }
    }
    /// The package manager lists a mounted system app a little after boot completes.
    fn await_package(&mut self) -> Result<(), String> {
        let deadline = Instant::now() + millis("PHONEFARM_INSTALL_BOOT_WAIT_MS", 300_000).min(Duration::from_secs(120));
        let command = format!("pm path --user 0 {} || true", q(&self.state.request.recipe.package));
        while self.read("installed_apk_path", &command)?.is_empty() {
            if Instant::now() >= deadline {
                return Err("the package did not appear after the module reboot".into());
            }
            std::thread::sleep(Duration::from_secs(3));
        }
        Ok(())
    }
    /// Undoes this session's module: one never mounted is deleted; a mounted one is marked for Magisk to remove at the
    /// next boot, and that boot must leave neither the module nor the package.
    fn remove_module(&mut self) -> Result<(), String> {
        self.reconcile_module_reboot()?;
        let Some(module) = self.state.module.clone() else {
            return Ok(());
        };
        let dir = q(&format!("{MODULES}/{}", module.id));
        let owned = format!("test -f {dir}/.phonefarm-owner && test \"$(cat {dir}/.phonefarm-owner)\" = {}", q(&self.state.token));
        let present = self.shell("module_present", &su(&format!("test -e {dir}")), 30)?.code == 0;
        if present && self.shell("module_owner", &su(&owned), 30)?.code != 0 {
            return Err(format!("the Magisk module {} is not this installation's; it was left in place", module.id));
        }
        if present && module.mounted_boot.is_none() {
            self.checked_write("remove_magisk_module", &su(&format!("rm -r {dir}")))?;
        } else if module.mounted_boot.is_some() {
            if present {
                self.checked_write("mark_magisk_module_removed", &su(&format!("touch {dir}/remove")))?;
            }
            self.reboot("module_removal_reboot")?;
            if self.shell("module_removed", &su(&format!("test ! -e {dir}")), 30)?.code != 0 {
                return Err(format!("the Magisk module {} is still present after the reboot", module.id));
            }
            self.absence()?;
        }
        self.state.checks["module_removed"] = json!({"id":module.id,"observed_at":now()});
        self.state.module = None;
        self.save()
    }
    fn cleanup_stage(&mut self) -> Result<(), String> {
        if !self.state.stage_attempted {
            return Ok(());
        }
        self.boot()?;
        let root = stage(&self.state.token);
        let absent = self.shell(
            "install_stage_absent",
            &format!("test ! -e {0} && test ! -L {0}", q(&root)),
            30,
        )?;
        if absent.code == 0 {
            return Ok(());
        }
        self.owner()?;
        let listing = self.read(
            "install_stage_inventory",
            &format!("cd {} && find . -mindepth 1 -print", q(&root)),
        )?;
        let entries: BTreeSet<_> = listing
            .lines()
            .map(|v| v.strip_prefix("./").unwrap_or(v))
            .collect();
        if !entries.contains(".phonefarm-owner")
            || entries
                .iter()
                .any(|v| !matches!(*v, ".phonefarm-owner" | "base.apk" | "base.apk.part" | "commit.result" | "commit.result.part"))
        {
            return Err("installation stage contains foreign entries".into());
        }
        for entry in entries {
            let file = q(&format!("{root}/{entry}"));
            self.read(
                "install_stage_type",
                &format!("test -f {file} && test ! -L {file}"),
            )?;
        }
        for name in ["base.apk.part", "base.apk", "commit.result.part", "commit.result"] {
            self.owner()?;
            let file = q(&format!("{root}/{name}"));
            self.checked_write("remove_install_apk",&format!("if test -e {file} || test -L {file}; then test -f {file} && test ! -L {file} && rm {file}; fi"))?;
        }
        self.owner()?;
        self.checked_write(
            "remove_install_stage",
            &format!(
                "rm {} && rmdir {}",
                q(&format!("{root}/.phonefarm-owner")),
                q(&root)
            ),
        )?;
        self.read(
            "install_stage_removed",
            &format!("test ! -e {0} && test ! -L {0}", q(&root)),
        )?;
        Ok(())
    }
    fn settle_uncommitted(&mut self) -> Result<(), String> {
        self.safe_to_reconcile()?;
        if self.state.commit_attempted && self.state.commit_outcome.is_none() {
            return Err(
                "commit was attempted; installation result requires explicit review".into(),
            );
        }
        self.remove_module()?;
        if self.state.create_attempted && !self.state.session_abandoned && !self.state.commit_attempted {
            let id = self.state.session_id.ok_or(
                "install-create did not leave an unambiguous session ID; cannot guess or abandon",
            )?;
            if !self.created_here {
                return Err("uncommitted installer session belongs to a previous process; current ownership cannot be verified, abandon refused".into());
            }
            let abandoned =
                self.write("install_abandon", &format!("pm install-abandon {id}"), 60)?;
            if abandoned.code != 0 || abandoned.stdout.trim() != "Success" {
                return Err("PackageInstaller did not confirm abandonment".into());
            }
            self.state.session_abandoned = true;
            self.save()?;
        }
        self.cleanup_stage()?;
        self.state.phase = "settled".into();
        self.save()
    }
    /// A module written but not yet seen mounted, as when the session stopped around its reboot: a new boot ID means
    /// the phone rebooted and Magisk mounted it; the same one means it never rebooted.
    fn reconcile_module_reboot(&mut self) -> Result<(), String> {
        if !self.state.module.as_ref().is_some_and(|m| m.written && m.mounted_boot.is_none())
            || !matches!(self.state.write_pending.as_deref(), None | Some("module_reboot"))
            || self.state.transport_uncertain
        {
            return Ok(());
        }
        let before = self.state.boot_id.clone().ok_or("device boot identity unavailable")?;
        let boot = self.probe_boot().ok_or(
            "the phone is not back from the module reboot; run recovery once it has booted",
        )?;
        if boot != before {
            self.state.boot_id = Some(boot.clone());
            self.module_mut()?.mounted_boot = Some(boot);
        }
        self.state.write_pending = None;
        self.save()
    }
    fn safe_to_reconcile(&self) -> Result<(), String> {
        if self.state.transport_uncertain || self.state.write_pending.is_some() {
            return Err(
                "unknown or pending installation command; automatic reconciliation refused".into(),
            );
        }
        Ok(())
    }
    fn report(&self, phase: &str, error: Option<&str>, status: &str) -> Value {
        let mut value = game_deploy::install_report(&self.state.request);
        game_deploy::report_binding(&mut value, self.state.device_binding.as_ref());
        value["phase"] = json!(phase);
        value["session_path"] = json!("game-deploy");
        value["error"] = json!(error);
        value["checks"] = self.state.checks.clone();
        if self.state.checks["apk_metadata"].is_object() {
            value["apk"] = json!({"status":"verified","scope":"host_metadata_only",
                "metadata":self.state.checks["apk_metadata"]});
        }
        if self.state.checks["installed"].is_object() {
            value["installed"] = self.state.checks["installed"].clone();
        }
        value["installer"] = json!({"session_id":self.state.session_id,"abandoned":self.state.session_abandoned,
            "commit_attempted":self.state.commit_attempted,"commit_confirmed":self.state.commit_confirmed,
            "package_retained":self.state.commit_confirmed || self.state.module_verified(),
            "root":self.state.root,"commit_outcome":self.state.commit_outcome,
            "method":self.state.checks["installed"]["method"],"module":self.state.module.as_ref().map(|m| &m.id)});
        value["recovery"] = json!({"status":status,"scope":INSTALL_SCOPE,"source_session":self.state.source_session,
            "context_moved":self.state.source_session.as_ref().map(|_|true),"device_restored":"not_claimed"});
        value
    }
    fn finish(
        &mut self,
        phase: &str,
        error: Option<&str>,
        settled: bool,
        code: i32,
    ) -> Result<(Value, i32), String> {
        let mut value = self.report(phase, error, if settled { "verified" } else { "required" });
        game_deploy::save_report(&self.out, &value)?;
        if settled {
            if let Err(e) = self.lease.complete_recovery(
                INSTALL_SCOPE,
                "device",
                &[&self.out.join("deployment.json")],
            ) {
                let _ = self.lease.hold_for_recovery(&self.dir);
                value["phase"] = json!("recovery_required");
                value["error"] = json!(e);
                value["recovery"]["status"] = json!("required");
                game_deploy::save_report(&self.out, &value)?;
                return Ok((value, 2));
            }
        }
        Ok((value, code))
    }
    pub(crate) fn execute(mut self) -> Result<(Value, i32), String> {
        unsafe {
            libc::signal(libc::SIGINT, interrupt as *const () as libc::sighandler_t);
        }
        if let Err(error) = self.preflight() {
            let settled = self.safe_to_reconcile().is_ok();
            return self.finish(
                if settled {
                    "preflight_failed"
                } else {
                    "recovery_required"
                },
                Some(&error),
                settled,
                2,
            );
        }
        if self.state.request.mode == "check" {
            self.state.phase = "checked".into();
            self.save()?;
            return self.finish("checked", None, true, 0);
        }
        match self.apply() {
            Ok(()) => self.finish("committed", None, true, 0),
            Err(error) => {
                let restore = if self.state.commit_attempted && self.state.commit_outcome.is_none() {
                    Err("commit was attempted; retain package and pending evidence for explicit reconciliation".into())
                } else {
                    self.settle_uncommitted()
                };
                match restore {
                    Ok(()) => self.finish("restored", Some(&error), true, 2),
                    Err(recovery) => {
                        let _ = self.lease.hold_for_recovery(&self.dir);
                        self.finish(
                            "recovery_required",
                            Some(&format!("{error}; recovery: {recovery}")),
                            false,
                            2,
                        )
                    }
                }
            }
        }
    }
    pub(crate) fn recover(source: &Path, serial: &str, out: &Path) -> Result<(Value, i32), String> {
        let source = source.canonicalize().map_err(|e| e.to_string())?;
        let bytes = crate::confirmation_transport::bounded(&source.join("session.json"), 131072)?;
        let mut state: State = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
        state.validate()?;
        if state.request.serial != serial {
            return Err("installation recovery serial differs from source".into());
        }
        let lease = match game_deploy::acquire_context(
            &source,
            serial,
            &bytes,
            state.device_binding.as_ref(),
        ) {
            Ok(lease) => lease,
            Err(error) => return failed_recovery(&state, &source, out, &error, Some(false)),
        };
        state.source_session = Some(source.clone());
        let prior = state.clone();
        let mut session = match Self::from_state(lease, state, out) {
            Ok(v) => v,
            Err(e) => return failed_recovery(&prior, &source, out, &e, Some(false)),
        };
        if let Err(error) = session.lease.hold_for_recovery(&session.dir) {
            let moved = game_deploy::context_moved(&session.lease, serial, &source, &session.dir);
            return failed_recovery(&prior, &source, out, &error, moved);
        }
        let result = (|| {
            session.reconcile_module_reboot()?;
            session.safe_to_reconcile()?;
            if session.state.commit_confirmed || session.state.module_verified() {
                session.verify_installed()?;
                session.cleanup_stage()?;
                session.state.phase = "settled".into();
                session.save()
            } else if session.state.module.as_ref().is_some_and(|m| m.mounted_boot.is_some()) {
                // Mounted, but the interrupted session never confirmed the package: confirm it, else remove it.
                match session.verify_installed() {
                    Ok(()) => {
                        session.module_mut()?.verified = true;
                        session.cleanup_stage()?;
                        session.state.phase = "settled".into();
                        session.save()
                    }
                    Err(_) => session.settle_uncommitted(),
                }
            } else {
                session.settle_uncommitted()
            }
        })();
        match result {
            Ok(()) => {
                let phase = if session.state.commit_confirmed || session.state.module_verified() {
                    "committed"
                } else {
                    "restored"
                };
                session.finish(phase, None, true, 0)
            }
            Err(error) => session.finish("recovery_required", Some(&error), false, 2),
        }
    }
}
fn failed_recovery(
    state: &State,
    source: &Path,
    out: &Path,
    error: &str,
    moved: Option<bool>,
) -> Result<(Value, i32), String> {
    let mut value = game_deploy::install_report(&state.request);
    game_deploy::report_binding(&mut value, state.device_binding.as_ref());
    value["phase"] = json!("recovery_required");
    value["error"] = json!(error);
    value["session_path"] = if moved == Some(true) {
        json!("game-deploy")
    } else {
        Value::Null
    };
    value["recovery"] = json!({"status":"required","scope":INSTALL_SCOPE,"source_session":source,"context_moved":moved});
    if let Err(e) = game_deploy::save_report(out, &value) {
        value["report_write_error"] = json!(e);
    }
    Ok((value, 2))
}
fn su(command: &str) -> String {
    format!("su -c {}", q(command))
}
/// The background commit's answer: the package manager's output and its exit status.
fn parse_commit(answer: &str) -> Result<(i32, String), String> {
    let (body, last) = answer.trim_end().rsplit_once('\n').unwrap_or(("", answer.trim_end()));
    let code = last
        .strip_prefix("PF_COMMIT_EXIT:")
        .and_then(|v| v.parse::<i32>().ok())
        .ok_or("the commit's answer has no exit status")?;
    Ok((code, body.trim().to_owned()))
}
fn require_absent(output: &str, package: &str) -> Result<(), String> {
    let mut android = false;
    for line in output.lines() {
        let name = line
            .strip_prefix("package:")
            .filter(|v| crate::graylayer::valid_package(v))
            .ok_or("package listing is incomplete or contains diagnostics")?;
        if name == package {
            return Err("package already installed or retained; replacement is forbidden".into());
        }
        android |= name == "android";
    }
    if !android {
        return Err("package listing has no Android framework sentinel; absence unknown".into());
    }
    Ok(())
}
fn require_installer(help: &str) -> Result<(), String> {
    let no_replace = help.lines().any(|line| {
        let line = line.trim();
        line.starts_with("-R:") && line.contains("disallow replacement of existing application")
    });
    if !no_replace
        || [
            "install-create",
            "install-write",
            "install-commit",
            "install-abandon",
        ]
        .iter()
        .any(|command| {
            !help
                .lines()
                .any(|line| line.trim_start().starts_with(command))
        })
    {
        return Err(
            "PackageManager does not explicitly advertise -R and required session commands".into(),
        );
    }
    Ok(())
}
fn parse_session_id(output: &str) -> Result<u32, String> {
    output
        .trim()
        .strip_prefix("Success: created install session [")
        .and_then(|v| v.strip_suffix(']'))
        .filter(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|v| v.parse::<u32>().ok())
        .filter(|v| *v > 0 && *v <= i32::MAX as u32)
        .ok_or_else(|| "install-create did not return an unambiguous session ID".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn installation_admission_and_results_do_not_guess() {
        let help=" -R: disallow replacement of existing application\n install-create [-S]\n install-write\n install-commit\n install-abandon\n";
        require_installer(help).unwrap();
        assert!(require_installer(&help.replace("-R:", "-r:")).is_err());
        require_absent(
            "package:android\npackage:com.other.game",
            "com.example.game",
        )
        .unwrap();
        assert!(require_absent("", "com.example.game").is_err());
        assert!(require_absent(
            "package:android\nError: user access denied",
            "com.example.game"
        )
        .is_err());
        assert!(require_absent(
            "package:android\npackage:com.example.game",
            "com.example.game"
        )
        .is_err());
        assert_eq!(
            parse_session_id("Success: created install session [17]\n").unwrap(),
            17
        );
        assert!(parse_session_id("Warning\nSuccess: created install session [17]").is_err());
        assert!(parse_session_id("Success: created install session [-1]").is_err());
    }
    #[test]
    fn a_background_commit_answer_and_module_names_are_read_exactly() {
        assert_eq!(parse_commit("Success\nPF_COMMIT_EXIT:0\n").unwrap(), (0, "Success".into()));
        assert_eq!(
            parse_commit("Failure [INSTALL_FAILED_ABORTED: -99]\nPF_COMMIT_EXIT:1").unwrap(),
            (1, "Failure [INSTALL_FAILED_ABORTED: -99]".into())
        );
        assert!(parse_commit("Success").is_err());
        assert_eq!(module_id("com.miHoYo.Yuanshen"), "pf_game_com_miHoYo_Yuanshen");
        assert_eq!(abi_dir("arm64-v8a"), Some("arm64"));
        assert_eq!(abi_dir("mips"), None);
        assert_eq!(su("pm install-commit 3"), "su -c 'pm install-commit 3'");
    }
}
