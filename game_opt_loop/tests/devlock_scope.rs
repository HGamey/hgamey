#![cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

#[test]
fn optimize_resolves_one_device_before_outer_lock_but_keeps_multi_device_global() {
    let root = std::env::temp_dir().join(format!("gol-scope-{}", std::process::id()));
    std::fs::create_dir_all(&root).unwrap();
    let pf = root.join("phonefarm");
    let lock = root.join("devlock");
    let seen = root.join("binding");
    let binding = r#"{"schema":"phonefarm/device-binding@1","domain_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","registry_revision":1,"device_id":"A","serial":"fake-a"}"#;
    std::fs::write(&pf, format!("#!/bin/sh\n[ \"$*\" = 'lease resolve --serial fake-a --json' ] || exit 2\nprintf '%s\\n' '{binding}'\n")).unwrap();
    std::fs::write(&lock, "#!/bin/sh\nprintf '%s' \"$PF_JOB_DEVICE_BINDING\" > \"$SCOPE_SEEN\"\nexit 2\n").unwrap();
    for file in [&pf, &lock] {
        std::fs::set_permissions(file, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    for multi in [false, true] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"));
        command.current_dir(&root).env("DEVLOCK_BIN", &lock).env("SCOPE_SEEN", &seen)
            .env_remove("PF_JOB_DEVICE_BINDING").env_remove("DEVLOCK_INSTANCE_ID")
            .args(["optimize", "--game", "fixture", "--device", "fake-a", "--plan", "--no-llm", "--phonefarm"])
            .arg(&pf).arg("--out").arg(root.join("out"));
        if multi { command.args(["--extra-device", "fake-b"]); }
        let output = command.output().unwrap();
        assert!(!output.status.success(), "fixture deliberately stops at the outer gate");
        let actual = std::fs::read_to_string(&seen).unwrap();
        if multi {
            assert_eq!(actual, "", "multi-device optimization must take the global scope");
        } else {
            assert_eq!(serde_json::from_str::<serde_json::Value>(&actual).unwrap(),
                serde_json::from_str::<serde_json::Value>(binding).unwrap());
        }
    }
    std::fs::remove_dir_all(&root).unwrap();
}
