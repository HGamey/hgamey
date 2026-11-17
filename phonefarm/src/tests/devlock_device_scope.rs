//! `tools/devlock run --serial` holds one phone's lane: real host processes and kernel locks, three registered phones.
#![cfg(unix)]
use serde_json::{json, Value};
use std::fs;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

struct Farm(PathBuf);
impl Farm {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "pf-devlock-scope-{}-{}",
            std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
        ));
        fs::create_dir_all(root.join("locks")).unwrap();
        let farm = Self(root);
        for (revision, id) in ["a", "b", "c"].into_iter().enumerate() {
            let request = farm.0.join("register.json");
            fs::write(&request, json!({"expected_revision": revision, "device": {
                "device_id": id, "label": id, "aliases": [format!("phone-{id}")], "enabled": true
            }}).to_string()).unwrap();
            let out = farm.env(Command::new(env!("CARGO_BIN_EXE_phonefarm")))
                .args(["lease", "devices-set", "--request"]).arg(request).arg("--json")
                .output().unwrap();
            assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
        }
        farm
    }
    fn env(&self, mut command: Command) -> Command {
        command
            .env("DEVLOCK_DIR", self.0.join("locks"))
            .env("PHONEFARM_LEASE_DIR", self.0.join("devices"))
            .env("PHONEFARM_BIN", env!("CARGO_BIN_EXE_phonefarm"))
            .env("DEVLOCK_WAIT_SECONDS", "0");
        for name in ["DEVLOCK_LABEL", "DEVLOCK_INSTANCE_ID", "PF_JOB_DEVICE_BINDING", "PF_JOB_ACTOR",
                     "PF_JOB_ID", "PF_JOB_LEASE_DIR", "PF_LOCK_FDS", "PF_LOCK_SCOPE"] {
            command.env_remove(name);
        }
        command
    }
    /// The node's job command: `devlock run job-<id> [--serial S] -- COMMAND`.
    fn run(&self, job: &str, serial: Option<&str>, program: &[&str]) -> Command {
        let mut command = self.env(Command::new(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../tools/devlock")));
        let receipts = self.0.join("receipts").join(job);
        fs::create_dir_all(&receipts).unwrap();
        command.env("PF_JOB_ID", job).env("PF_JOB_LEASE_DIR", receipts).args(["run", &format!("job-{job}")]);
        if let Some(serial) = serial {
            command.args(["--serial", serial]);
        }
        command.arg("--").args(program);
        command
    }
    fn until(&self, name: &str) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !self.0.join(name).exists() {
            assert!(Instant::now() < deadline, "holder never started: {}", self.0.display());
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Farm {
    fn drop(&mut self) {
        if !std::thread::panicking() {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
}
struct Group(Child);
impl Drop for Group {
    fn drop(&mut self) {
        unsafe {
            libc::kill(-(self.0.id() as i32), libc::SIGKILL);
        }
        let _ = self.0.wait();
    }
}

#[test]
fn a_job_holding_one_phone_leaves_the_other_two_free_and_is_named_as_the_holder() {
    let farm = Farm::new();
    let hold = "touch \"$1/ready\"; while [ ! -f \"$1/finish\" ]; do sleep .02; done";
    let root = farm.0.to_str().unwrap().to_owned();
    let mut holder = Group(
        farm.run("00000000aaaa", Some("phone-a"), &["/bin/sh", "-c", hold, "holder", &root])
            .process_group(0)
            .spawn()
            .unwrap(),
    );
    farm.until("ready");
    for (job, serial) in [("00000000bbbb", "phone-b"), ("00000000cccc", "phone-c")] {
        let out = farm.run(job, Some(serial), &["/usr/bin/true"]).output().unwrap();
        assert!(out.status.success(), "{serial} waited for phone a: {}", String::from_utf8_lossy(&out.stderr));
    }
    let same = farm.run("00000000aaab", Some("phone-a"), &["/usr/bin/true"]).output().unwrap();
    assert!(!same.status.success(), "a second job entered phone a while it was held");
    let farm_wide = farm.run("00000000ffff", None, &["/usr/bin/true"]).output().unwrap();
    assert!(!farm_wide.status.success(), "a call naming no phone still needs every phone free");

    let status = farm.env(Command::new(env!("CARGO_BIN_EXE_phonefarm"))).args(["lease", "status", "--json"])
        .output().unwrap();
    let status: Value = serde_json::from_slice(&status.stdout).unwrap();
    let held: Vec<_> = status["entries"].as_array().unwrap().iter()
        .map(|entry| (entry["scope"].as_str().unwrap(), entry["device_id"].as_str(), entry["job_id"].as_str()))
        .collect();
    assert_eq!(held, [("device", Some("a"), Some("00000000aaaa"))], "{status}");
    assert_eq!(status["device_scopes"]["global"]["status"], "clear", "{status}");
    let lanes: Vec<_> = status["device_scopes"]["devices"].as_array().unwrap().iter()
        .map(|device| (device["device_id"].as_str().unwrap(), device["status"].as_str().unwrap()))
        .collect();
    assert_eq!(lanes, [("a", "held"), ("b", "clear"), ("c", "clear")]);

    fs::write(farm.0.join("finish"), b"").unwrap();
    assert!(holder.0.wait().unwrap().success());
    assert!(farm.run("00000000aaab", Some("phone-a"), &["/usr/bin/true"]).output().unwrap().status.success());
}
