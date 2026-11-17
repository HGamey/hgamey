//! games install on a rooted phone, against the simulated phone in support/fake_install_adb.py (never real-device
//! data): a root install skips ColorOS's approval screens; a commit refused for policy or left waiting becomes a Magisk
//! module mounted by a reboot; a module that does not mount is removed with a reboot; and a genuine failure, or a phone
//! without root Magisk, is reported and restored.
#![cfg(unix)]
use serde_json::{json, Value};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT: AtomicU64 = AtomicU64::new(0);
const PACKAGE: &str = "com.example.colorosgame";
const MODULE: &str = "pf_game_com_example_colorosgame";

struct Phone {
    root: PathBuf,
    apk: Value,
    env: Vec<(&'static str, String)>,
}
impl Phone {
    fn new(env: &[(&'static str, &str)]) -> Self {
        let root = std::env::temp_dir().join(format!(
            "phonefarm-game-install-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(root.join("cache")).unwrap();
        for (name, tool) in [("adb", "adb"), ("aapt2", "aapt2")] {
            let path = root.join(name);
            std::fs::write(&path, format!("#!/bin/sh\nexec python3 {} {tool} \"$@\"\n", fake().display())).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let made = Command::new("python3")
            .arg(fake())
            .args(["make-apk", root.join("cache/game.apk").to_str().unwrap(), PACKAGE, "637"])
            .output()
            .unwrap();
        assert!(made.status.success(), "{made:?}");
        let apk = serde_json::from_slice(&made.stdout).unwrap();
        let env = env.iter().map(|(k, v)| (*k, v.to_string())).collect();
        Self { root, apk, env }
    }
    fn install(&self, run: &str) -> (Value, i32) {
        let request = self.request(run);
        self.phonefarm(&["--request", request.to_str().unwrap(), "--serial", "fixture-phone", "--out",
            self.root.join(run).to_str().unwrap(), "--json"])
    }
    fn recover(&self, run: &str, into: &str) -> (Value, i32) {
        let session = self.root.join(run).join("game-deploy");
        self.phonefarm(&["recover", "--session", session.to_str().unwrap(), "--serial", "fixture-phone", "--out",
            self.root.join(into).to_str().unwrap(), "--json"])
    }
    fn request(&self, run: &str) -> PathBuf {
        let request = json!({"schema":"phonefarm/game-install-request@1","node_id":"fixture-node","game_id":"gi-pilot",
            "version":"v4","serial":"fixture-phone","mode":"apply",
            "recipe":{"schema":"phonefarm/game-install-recipe@1","package":PACKAGE,"version_code":637,
                "apk_sha256":self.apk["sha256"],"apk_asset_name":"game.apk"},
            "assets":[{"name":"game.apk","path":self.root.join("cache/game.apk"),"sha256":self.apk["sha256"],"size":self.apk["size"]}]});
        let path = self.root.join(format!("{run}.json"));
        std::fs::write(&path, request.to_string()).unwrap();
        path
    }
    fn phonefarm(&self, args: &[&str]) -> (Value, i32) {
        let mut command = Command::new(env!("CARGO_BIN_EXE_phonefarm"));
        command
            .arg("game-deploy")
            .args(args)
            .current_dir(&self.root)
            .env("ADB_BIN", self.root.join("adb"))
            .env("AAPT2_BIN", self.root.join("aapt2"))
            .env("PF_FAKE_PHONE", self.root.join("phone"))
            .env("PHONEFARM_LEASE_DIR", self.root.join("leases"))
            .env("PHONEFARM_STATE_DIR", self.root.join("state"))
            .env("DEVLOCK_DIR", self.root.join("legacy"))
            .env("DEVLOCK_BIN", Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().join("tools/devlock"))
            .env("PHONEFARM_LEASE_TIMEOUT_MS", "1000")
            .env("PHONEFARM_INSTALL_COMMIT_WAIT_MS", "1500")
            .env("PHONEFARM_INSTALL_BOOT_WAIT_MS", "4000");
        for key in ["DEVLOCK_INSTANCE_ID", "PF_JOB_ID", "PF_JOB_LEASE_DIR", "PF_JOB_OUT", "PF_RECOVERY_OUT", "PF_RUNTIME_REPORT"] {
            command.env_remove(key);
        }
        for (key, value) in &self.env {
            command.env(key, value);
        }
        let output = command.output().unwrap();
        let stdout = String::from_utf8_lossy(&output.stdout);
        let report = stdout
            .lines()
            .rev()
            .find_map(|line| serde_json::from_str::<Value>(line).ok())
            .unwrap_or_else(|| panic!("no report: {stdout} {}", String::from_utf8_lossy(&output.stderr)));
        (report, output.status.code().unwrap())
    }
    fn file(&self, path: &str) -> PathBuf {
        self.root.join("phone").join(path)
    }
    fn log(&self, tool: &str) -> String {
        std::fs::read_to_string(self.file(&format!("{tool}.log"))).unwrap_or_default()
    }
    fn reboots(&self) -> u64 {
        let state: Value = serde_json::from_str(&std::fs::read_to_string(self.file("state.json")).unwrap()).unwrap();
        state["reboots"].as_u64().unwrap_or(0)
    }
    fn stage_left(&self) -> bool {
        std::fs::read_dir(self.file("data/local/tmp")).unwrap().any(|e| {
            e.unwrap().file_name().to_string_lossy().starts_with("pf-install-")
        })
    }
}
impl Drop for Phone {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}
fn fake() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/support/fake_install_adb.py")
}

#[test]
fn a_root_install_skips_the_approval_screens_a_shell_install_waits_on() {
    let phone = Phone::new(&[("PF_FAKE_ROOT", "1"), ("PF_FAKE_COMMIT", "root_only")]);
    let (report, code) = phone.install("install");
    assert_eq!((code, report["phase"].as_str()), (0, Some("committed")), "{report}");
    assert_eq!(report["installed"]["method"], "package_installer_root");
    assert_eq!(report["installed"]["apk_path"], format!("/data/app/{PACKAGE}/base.apk"));
    assert_eq!(report["installer"]["root"], true);
    assert_eq!(report["checks"]["installer"]["magisk_version"], "30700");
    assert_eq!(report["recovery"]["status"], "verified");
    assert!(phone.log("pm").contains("uid=0 install-commit 1"), "the commit runs as root");
    assert!(!phone.log("pm").contains("uid=2000 install-"));
    assert_eq!(phone.reboots(), 0);
    assert!(!phone.stage_left());
}

#[test]
fn a_commit_left_waiting_is_abandoned_and_the_apk_installed_as_a_magisk_module() {
    let phone = Phone::new(&[("PF_FAKE_ROOT", "1"), ("PF_FAKE_COMMIT", "waits")]);
    let (report, code) = phone.install("install");
    assert_eq!((code, report["phase"].as_str()), (0, Some("committed")), "{report}");
    let installed = &report["installed"];
    assert_eq!(installed["method"], "magisk_module");
    assert_eq!(installed["apk_path"], format!("/system/app/{MODULE}/base.apk"));
    assert_eq!(installed["version_code"], 637);
    assert_eq!(installed["apk_sha256"], phone.apk["sha256"]);
    assert_eq!(installed["module"]["uninstall"], format!("su -c 'touch /data/adb/modules/{MODULE}/remove', then reboot"));
    assert_eq!(report["installer"]["abandoned"], true);
    assert!(report["installer"]["commit_outcome"].as_str().unwrap().contains("abandoned"), "{report}");
    let module = phone.file(&format!("data/adb/modules/{MODULE}"));
    let prop = std::fs::read_to_string(module.join("module.prop")).unwrap();
    assert!(prop.starts_with(&format!("id={MODULE}\nname=gi-pilot v4\nversion=637\nversionCode=637\n")), "{prop}");
    assert!(module.join(format!("system/app/{MODULE}/lib/arm64/libgame.so")).exists(), "the phone's first ABI");
    assert!(!module.join(format!("system/app/{MODULE}/lib/arm")).exists());
    assert_eq!(phone.reboots(), 1);
    assert!(phone.log("pm").contains("uid=0 install-abandon 1"));
    assert!(phone.log("am").contains("force-stop com.oplus.appdetail"), "its install screens are closed");
    assert!(!phone.stage_left());

    // Installing it again names the module and how to uninstall it.
    let (again, code) = phone.install("again");
    assert_eq!(code, 2);
    assert!(again["error"].as_str().unwrap().contains(&format!("installed by the Magisk module {MODULE}: uninstall it with `su -c 'touch /data/adb/modules/{MODULE}/remove'` and a reboot")), "{again}");
}

#[test]
fn a_policy_refusal_becomes_a_module_but_a_genuine_failure_or_no_magisk_is_reported_and_restored() {
    let phone = Phone::new(&[("PF_FAKE_ROOT", "1"), ("PF_FAKE_COMMIT", "refused")]);
    let (report, code) = phone.install("install");
    assert_eq!((code, report["installed"]["method"].as_str()), (0, Some("magisk_module")), "{report}");
    assert!(report["installer"]["commit_outcome"].as_str().unwrap().contains("-99"));

    let phone = Phone::new(&[("PF_FAKE_ROOT", "1"), ("PF_FAKE_COMMIT", "storage")]);
    let (report, code) = phone.install("install");
    assert_eq!((code, report["phase"].as_str()), (2, Some("restored")), "{report}");
    assert!(report["error"].as_str().unwrap().contains("refused the APK (INSUFFICIENT_STORAGE)"), "{report}");
    assert!(!phone.file(&format!("data/adb/modules/{MODULE}")).exists());
    assert!(!phone.stage_left());

    let phone = Phone::new(&[("PF_FAKE_COMMIT", "refused")]);
    let (report, code) = phone.install("install");
    assert_eq!((code, report["phase"].as_str()), (2, Some("restored")), "{report}");
    assert!(report["error"].as_str().unwrap().contains("no root Magisk"), "{report}");
    assert_eq!(report["installer"]["root"], false);
    assert!(!phone.stage_left());
}

#[test]
fn a_module_that_does_not_mount_is_removed_with_a_reboot() {
    let phone = Phone::new(&[("PF_FAKE_ROOT", "1"), ("PF_FAKE_COMMIT", "waits"), ("PF_FAKE_MOUNT", "broken")]);
    let (report, code) = phone.install("install");
    assert_eq!((code, report["phase"].as_str()), (2, Some("restored")), "{report}");
    assert!(report["error"].as_str().unwrap().contains("did not appear after the module reboot"), "{report}");
    assert_eq!(report["checks"]["module_removed"]["id"], MODULE);
    assert!(!phone.file(&format!("data/adb/modules/{MODULE}")).exists());
    assert_eq!(phone.reboots(), 2, "one to mount the module, one to remove it");
    assert!(!phone.stage_left());
}

#[test]
fn a_phone_still_away_after_the_module_reboot_is_recovered_once_it_is_back() {
    let phone = Phone::new(&[("PF_FAKE_ROOT", "1"), ("PF_FAKE_COMMIT", "waits"), ("PF_FAKE_STAY_OFFLINE", "1")]);
    let (report, code) = phone.install("install");
    assert_eq!((code, report["phase"].as_str()), (2, Some("recovery_required")), "{report}");
    assert!(report["error"].as_str().unwrap().contains("not back from the module reboot; run recovery once it has booted"), "{report}");
    std::fs::remove_file(phone.file("offline")).unwrap();
    let (report, code) = phone.recover("install", "recovery");
    assert_eq!((code, report["phase"].as_str()), (0, Some("committed")), "{report}");
    assert_eq!(report["installed"]["method"], "magisk_module");
    assert_eq!(report["recovery"]["status"], "verified");
    assert_eq!(phone.reboots(), 1);
    assert!(!phone.stage_left());
}
