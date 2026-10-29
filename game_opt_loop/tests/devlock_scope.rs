#![cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

#[test]
fn optimize_resolves_one_device_before_outer_lock_but_keeps_multi_device_global() {
    let root = std::env::temp_dir().join(format!("gol-scope-{}", std::process::id()));
    std::fs::create_dir_all(&root).unwrap();
    let pf = root.join("phonefarm");
    let seen = root.join("binding");
    std::fs::write(&pf, "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$SCOPE_SEEN\"\nexit 2\n").unwrap();
    std::fs::set_permissions(&pf, std::fs::Permissions::from_mode(0o755)).unwrap();
    for multi in [false, true] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"));
        command.current_dir(&root).env("SCOPE_SEEN", &seen)
            .env_remove("PF_JOB_DEVICE_BINDING").env_remove("DEVLOCK_INSTANCE_ID")
            .args(["optimize", "--game", "fixture", "--device", "fake-a", "--plan", "--no-llm", "--phonefarm"])
            .arg(&pf).arg("--out").arg(root.join("out"));
        if multi { command.args(["--extra-device", "fake-b"]); }
        let output = command.output().unwrap();
        assert!(!output.status.success(), "fixture deliberately stops at the outer gate");
        let actual = std::fs::read_to_string(&seen).unwrap();
        let args: Vec<_> = actual.lines().collect();
        assert_eq!(args[0], "__lease-handoff");
        if multi {
            assert!(!args.contains(&"--serial"), "multiple devices require farm scope");
        } else {
            let index = args.iter().position(|arg| *arg == "--serial").unwrap();
            assert_eq!(args[index + 1], "fake-a");
        }
    }
    std::fs::remove_dir_all(&root).unwrap();
}
