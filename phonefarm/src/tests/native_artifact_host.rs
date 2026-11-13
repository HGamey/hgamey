use regex::Regex;
use serde_json::Value;
use std::{collections::BTreeSet, fs, os::unix::process::CommandExt, path::{Path, PathBuf}, process::Command};

/// Vulkan headers for the full layer tests: VULKAN_HEADERS_DIR, else the DevEco SDK (macOS), else the system's.
fn vulkan_headers() -> Option<PathBuf> {
    let candidates = match std::env::var("VULKAN_HEADERS_DIR") {
        Ok(dir) => vec![PathBuf::from(dir)],
        Err(_) => vec![
            PathBuf::from("/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/sysroot/usr/include"),
            PathBuf::from("/usr/include"),
        ],
    };
    candidates.into_iter().find(|dir| dir.join("vulkan/vulkan.h").exists())
}

// Isolate just Vulkan headers; do not compile against an OHOS sysroot on macOS.
fn link_vulkan_headers(headers: &Path, temp: &Path) {
    std::os::unix::fs::symlink(headers.join("vulkan"), temp.join("vulkan")).unwrap();
    if headers.join("vk_video").exists() {
        std::os::unix::fs::symlink(headers.join("vk_video"), temp.join("vk_video")).unwrap();
    }
}

fn temp_dir(prefix: &str) -> PathBuf {
    let temp = std::env::temp_dir().join(format!(
        "{prefix}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir(&temp).unwrap();
    temp
}

#[test]
fn host_native_counters_and_actual_layer_hooks() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../knobs/gray");
    let temp = temp_dir("phonefarm-native-artifact");
    let cxx = std::env::var("CXX").unwrap_or_else(|_| "c++".into());
    let exe = temp.join("stats-test");
    let output = Command::new(&cxx)
        .args(["-std=c++17", "-Wall", "-Wextra", "-Werror"])
        .arg(root.join("tests/artifact_stats_test.cpp"))
        .arg("-o")
        .arg(&exe)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let output = Command::new(&exe).output().unwrap();
    assert!(output.status.success());
    let stats: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(stats["queue_submitted"], 8);
    let Some(headers) = vulkan_headers() else {
        eprintln!("Full layer host-hook test not run: set VULKAN_HEADERS_DIR to existing Vulkan headers. Counter header test passed.");
        fs::remove_dir_all(&temp).unwrap();
        return;
    };
    link_vulkan_headers(&headers, &temp);
    fs::write(temp.join("copy_spv.h"),"alignas(4) static const unsigned char kCopySpv[]={3,2,35,7};static const unsigned int kCopySpvLen=4;alignas(4) static const unsigned char kUpopSpv[]={3,2,35,7};static const unsigned int kUpopSpvLen=4;alignas(4) static const unsigned char kNpuPackSpv[]={3,2,35,7};static const unsigned int kNpuPackSpvLen=4;alignas(4) static const unsigned char kNpuUnpackSpv[]={3,2,35,7};static const unsigned int kNpuUnpackSpvLen=4;").unwrap();
    for (dynamic, fixture) in [(false, true), (true, true), (false, false), (true, false)] {
        if dynamic {
            fs::write(temp.join("artifact_metadata.h"),"#define RK_DYNAMIC_ARTIFACT_METADATA 1\nstatic const char kArtifactId[]=\"upop-host-fixture\";static const char kCandidateSourceSha256[]=\"source-fixture\";static const char kCandidateSpirvSha256[]=\"spv-fixture\";\n").unwrap();
        }
        let mut cmd = Command::new(&cxx);
        cmd.args(["-std=c++17", "-fno-exceptions", "-fno-rtti", "-I"])
            .arg(&temp)
            .arg("-I")
            .arg(root.join("tests/stubs"));
        if dynamic {
            cmd.arg("-DRK_DYNAMIC_UPOP_BUILD=1");
        }
        if fixture {
            cmd.arg("-DRK_LIFETIME_HOST_FIXTURE=1");
        }
        let exe = temp.join(format!("layer-{dynamic}-{fixture}-test"));
        let output = cmd
            .arg(root.join("tests/layer_artifact_test.cpp"))
            // main 的层把 present 钩子接到 npuprobe (build_layer.sh 一起编), 宿主测试同样链进来
            .arg(root.join("layer/npuprobe.cpp"))
            // ruler 侧 framedump 档 (build_layer.sh 一起编), 宿主测试同样链进来
            .arg(root.join("layer/framedump.cpp"))
            // interp (I6 层内多 present), build_layer.sh 一起编
            .arg(root.join("layer/interp.cpp"))
            // selfsr (送显前超分), build_layer.sh 一起编; 没有 selfsr_spv.h 时模块自己降级
            .arg(root.join("layer/selfsr.cpp"))
            .arg("-o")
            .arg(&exe)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let output = Command::new(&exe).output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let marker: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(
            marker["artifact_id"],
            if dynamic {
                "upop-host-fixture"
            } else {
                "legacy-fixed-upop"
            }
        );
        assert_eq!(marker["deployable"], false);
        assert_eq!(
            marker["native_marker"]["upop_artifact"]["ownership_scope"],
            "recording_generation_per_dispatch"
        );
        assert_eq!(
            marker["native_marker"]["upop_artifact"]["evaluation_eligible"],
            false
        );
        assert_eq!(
            marker["native_marker"]["upop_artifact"]["gpu_completion_proven"],
            false
        );
        if fixture {
            assert!(marker["ownership_cases"].as_u64().unwrap() >= 39);
            assert!(marker["dispatch_recorded"].as_u64().unwrap() >= 141);
        } else {
            assert_eq!(marker["dispatch_recorded"], 0);
            assert_eq!(marker["ownership_cases"], 1);
        }
    }
    let android_fixture = Command::new(&cxx)
        .args([
            "-std=c++17",
            "-D__ANDROID__=1",
            "-DRK_LIFETIME_HOST_FIXTURE=1",
            "-fsyntax-only",
            "-I",
        ])
        .arg(&temp)
        .arg("-I")
        .arg(root.join("tests/stubs"))
        .arg(root.join("layer/vk_layer_refknobs.cpp"))
        .output()
        .unwrap();
    assert!(!android_fixture.status.success());
    assert!(String::from_utf8_lossy(&android_fixture.stderr)
        .contains("Host lifetime contracts are forbidden"));
    fs::remove_file(temp.join("artifact_metadata.h")).unwrap();
    let output = Command::new(&cxx)
        .args([
            "-std=c++17",
            "-DRK_DYNAMIC_UPOP_BUILD=1",
            "-fsyntax-only",
            "-I",
        ])
        .arg(&temp)
        .arg("-I")
        .arg(root.join("tests/stubs"))
        .arg(root.join("layer/vk_layer_refknobs.cpp"))
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "dynamic build must fail without metadata"
    );
    fs::remove_dir_all(temp).unwrap();
}

// The gray session's debug.vulkan.layers is global, so other apps (RedMagic's gameassist) and the
// target's own :subprocesses load the layer too. Only the process named exactly like
// debug.knobs.target may read knobs or hook anything; every other one must look like no layer.
#[test]
fn processes_other_than_the_session_target_see_a_transparent_layer() {
    let Some(headers) = vulkan_headers() else {
        eprintln!("Layer pass-through test not run: set VULKAN_HEADERS_DIR to existing Vulkan headers.");
        return;
    };
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../knobs/gray");
    let temp = temp_dir("phonefarm-layer-passthrough");
    link_vulkan_headers(&headers, &temp);
    let sources = ["vk_layer_refknobs.cpp", "npuprobe.cpp", "framedump.cpp", "interp.cpp", "selfsr.cpp"];
    let (mut names, mut knobs) = (BTreeSet::new(), BTreeSet::new());
    let vk = Regex::new(r#""(vk[A-Z][A-Za-z0-9]*)""#).unwrap();
    let knob = Regex::new(r#""(debug\.knobs\.[a-z0-9_]+)""#).unwrap();
    for file in sources {
        let text = fs::read_to_string(root.join("layer").join(file)).unwrap();
        names.extend(vk.captures_iter(&text).map(|c| c[1].to_owned()));
        knobs.extend(knob.captures_iter(&text).map(|c| c[1].to_owned()));
    }
    for expected in ["vkQueuePresentKHR", "vkCmdBeginRenderPass", "vkCreateSwapchainKHR", "vkQueueSubmit", "vkCreateImage"] {
        assert!(names.contains(expected), "{expected} missing from {names:?}");
    }
    for expected in ["debug.knobs.framedump", "debug.knobs.framedump_dir", "debug.knobs.passdump", "debug.knobs.npuprobe"] {
        assert!(knobs.contains(expected), "{expected} missing from {knobs:?}");
    }
    let array = |name: &str, items: &BTreeSet<String>| {
        let items: Vec<_> = items.iter().map(|s| format!("\"{s}\"")).collect();
        format!("static const char *const {name}[] = {{{}}};\n", items.join(","))
    };
    fs::write(temp.join("layer_vk_names.h"), array("kLayerVkNames", &names)).unwrap();
    fs::write(temp.join("layer_knob_props.h"), array("kLayerKnobProps", &knobs)).unwrap();
    let exe = temp.join("layer-passthrough-test");
    let cxx = std::env::var("CXX").unwrap_or_else(|_| "c++".into());
    let mut cmd = Command::new(&cxx);
    cmd.args(["-std=c++17", "-fno-exceptions", "-fno-rtti", "-I"])
        .arg(&temp)
        .arg("-I")
        .arg(root.join("tests/stubs"))
        .arg(root.join("tests/layer_passthrough_test.cpp"));
    for file in &sources[1..] {
        cmd.arg(root.join("layer").join(file));
    }
    let output = cmd.arg("-o").arg(&exe).output().unwrap();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    for (process, target, transparent) in [
        ("cn.nubia.gameassist", "com.miHoYo.Yuanshen", true),
        ("com.miHoYo.Yuanshen:push", "com.miHoYo.Yuanshen", true),
        ("com.miHoYo.Yuanshen", "com.miHoYo.Yuanshen", false),
        // Without a session target (property empty, or 0 after `gray-layer off`) the layer works as before.
        ("com.miHoYo.Yuanshen", "", false),
        ("cn.nubia.gameassist", "0", false),
    ] {
        let cwd = temp.join(format!("cwd-{}", process.replace(':', "-") + "-" + if target.is_empty() { "none" } else { target }));
        fs::create_dir(&cwd).unwrap();
        let output = Command::new(&exe)
            .arg0(process)
            .arg(target)
            .arg(if transparent { "transparent" } else { "target" })
            .current_dir(&cwd)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{process} with target {target:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["process"], process);
        assert_eq!(report["transparent"], transparent);
        if transparent {
            assert_eq!(report["vk_names"], names.len());
            assert_eq!(report["prop_reads"], 1);
            // Every knob is set to 1 (framedump_dir is then the relative path "1"): nothing may appear.
            assert_eq!(fs::read_dir(&cwd).unwrap().count(), 0, "{process} wrote into its working directory");
        }
    }
    fs::remove_dir_all(temp).unwrap();
}
