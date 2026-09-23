//! 只读 HTTP 服务。
//!
//! 三条硬规矩:
//! 1. **只读**。没有任何写接口, 不碰设备, 不碰设备锁。
//! 2. **离线**。前端资源 `include_str!` 进二进制, 没有一个外网请求。
//! 3. **路径白名单**。`/api/file` 只肯交出两个仓库根之下的文件, 先
//!    `canonicalize` 再比前缀 —— 否则 `?path=../../.ssh/id_rsa` 这种就通了。

use crate::viz::{Dashboard, VizPaths};
use axum::extract::{Query, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde::Deserialize;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

const INDEX_HTML: &str = include_str!("assets/index.html");
const APP_CSS: &str = include_str!("assets/app.css");
const APP_JS: &str = include_str!("assets/app.js");

/// `/api/file` 单个文件的上限。
///
/// 卡这个数的**具体原因**: 运行目录里躺着 `trace.txt`, 动辄 50 MB 的原始 ftrace。
/// 没有上限的话, 演示时手一滑点到它, 浏览器直接卡死。
const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024;

/// `/api/file` 肯交出的扩展名。
///
/// 白名单而不是黑名单, 原因很具体: phonefarm 根目录下躺着 `secrets.env`
/// (LLM 密钥, 就是 `--secrets` 的缺省值), 还有 `.git` 里的凭据。
/// 面板只需要那几类证据文件, 那就只给那几类 —— 不必去猜下一个敏感文件叫什么。
const READABLE_EXTENSIONS: &[&str] = &[
    "json", "jsonl", "md", "txt", "log", "tsv", "csv", "png", "jpg", "jpeg", "glsl", "spvasm",
];

#[derive(Clone)]
struct AppState {
    paths: Arc<VizPaths>,
}

/// 起服务并阻塞, 直到 Ctrl-C。
pub fn run(paths: VizPaths, port: u16) -> anyhow::Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    runtime.block_on(serve(paths, port))
}

async fn serve(paths: VizPaths, port: u16) -> anyhow::Result<()> {
    // 只绑回环地址: 这是给本机演示用的只读面板, 不该出现在局域网上。
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let listener = tokio::net::TcpListener::bind(addr).await?;
    // 把读的是哪几个目录打出来 —— 演示时最怕"页面开了, 但数据是别处那份"。
    println!("面板已启动: http://127.0.0.1:{port}   (Ctrl-C 退出)");
    println!("  层①  算子   ← {}", paths.gol_root.display());
    println!("  层②③ 真机   ← {}", paths.phonefarm_root.display());
    println!("  设备锁       ← {}", paths.lock_root.display());

    let state = AppState {
        paths: Arc::new(paths),
    };
    axum::serve(listener, router(state)).await?;
    Ok(())
}

fn router(state: AppState) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/app.css", get(css))
        .route("/app.js", get(js))
        .route("/healthz", get(|| async { "ok" }))
        .route("/api/dashboard", get(dashboard))
        .route("/api/file", get(file))
        .with_state(state)
}

fn no_store() -> HeaderMap {
    let mut h = HeaderMap::new();
    // 面板 30 秒自刷新, 缓存只会让人看着旧数据下判断。
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    h
}

async fn index() -> impl IntoResponse {
    (
        no_store(),
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        INDEX_HTML,
    )
}

async fn css() -> impl IntoResponse {
    (
        no_store(),
        [(header::CONTENT_TYPE, "text/css; charset=utf-8")],
        APP_CSS,
    )
}

async fn js() -> impl IntoResponse {
    (
        no_store(),
        [(
            header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        APP_JS,
    )
}

/// 每次请求都重新读盘 —— 数据量小 (几百 KB), 而"看到的一定是盘上现在这份"
/// 比省几毫秒重要得多。
async fn dashboard(State(state): State<AppState>) -> Response {
    let paths = state.paths.clone();
    let built = tokio::task::spawn_blocking(move || crate::viz::build(&paths)).await;
    match built {
        Ok(dash) => (no_store(), Json::<Dashboard>(dash)).into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("读数据时崩了: {e}"),
        )
            .into_response(),
    }
}

#[derive(Deserialize)]
struct FileQuery {
    path: String,
}

/// 把请求的路径收敛到白名单之内。
///
/// 返回 `Err` 的三种情况都要分得清:
/// - 不在白名单 → 403 (不是 404: 文件可能真的在, 只是不该给)
/// - 不存在 → 404
/// - 太大 → 413
pub fn resolve_allowed(raw: &str, allowed_roots: &[PathBuf]) -> Result<PathBuf, (StatusCode, String)> {
    let requested = Path::new(raw);
    let canonical = requested
        .canonicalize()
        .map_err(|e| (StatusCode::NOT_FOUND, format!("{raw}: {e}")))?;
    if !allowed_roots.iter().any(|root| canonical.starts_with(root)) {
        return Err((
            StatusCode::FORBIDDEN,
            "这个路径不在允许读取的仓库目录之内".to_string(),
        ));
    }
    // 点开头的目录 (`.git` / `.cargo` …) 里没有面板要的东西, 却有一堆不该外传的。
    if canonical
        .components()
        .any(|c| c.as_os_str().to_string_lossy().starts_with('.'))
    {
        return Err((
            StatusCode::FORBIDDEN,
            "隐藏目录里的文件不对面板开放".to_string(),
        ));
    }
    // 先判在不在、是不是文件 (给 404), 再判类型 (给 403) —— 两种拒绝要分得清。
    if !canonical.is_file() {
        return Err((StatusCode::NOT_FOUND, "不是一个文件".to_string()));
    }
    let ext = canonical
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !READABLE_EXTENSIONS.contains(&ext.as_str()) {
        return Err((
            StatusCode::FORBIDDEN,
            format!("面板只读证据类文件 ({}), 不读 .{ext}", READABLE_EXTENSIONS.join("/")),
        ));
    }
    let size = std::fs::metadata(&canonical)
        .map_err(|e| (StatusCode::NOT_FOUND, e.to_string()))?
        .len();
    if size > MAX_FILE_BYTES {
        return Err((
            StatusCode::PAYLOAD_TOO_LARGE,
            format!(
                "文件 {:.1} MB, 超过面板上限 {:.0} MB (原始 trace 请用命令行看)",
                size as f64 / 1_048_576.0,
                MAX_FILE_BYTES as f64 / 1_048_576.0
            ),
        ));
    }
    Ok(canonical)
}

fn content_type_for(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase()
        .as_str()
    {
        "json" => "application/json; charset=utf-8",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        _ => "text/plain; charset=utf-8",
    }
}

async fn file(State(state): State<AppState>, Query(q): Query<FileQuery>) -> Response {
    // 每次现算白名单, 不用启动时的快照: 面板起在前、phonefarm 挂在后的话,
    // 快照会把层②③ 的证据文件一直拒到重启为止。
    let roots = state.paths.allowed_roots();
    let resolved = match resolve_allowed(&q.path, &roots) {
        Ok(p) => p,
        Err((code, msg)) => return (code, msg).into_response(),
    };
    match std::fs::read(&resolved) {
        Ok(bytes) => (
            no_store(),
            [(header::CONTENT_TYPE, content_type_for(&resolved))],
            bytes,
        )
            .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).canonicalize().unwrap()
    }

    #[test]
    fn allows_an_evidence_file_inside_the_root() {
        let roots = vec![manifest()];
        let p = manifest().join("evidence/M1_report.json");
        assert!(resolve_allowed(p.to_str().unwrap(), &roots).is_ok());
    }

    /// phonefarm 根下就躺着 `secrets.env` (LLM 密钥)。面板绝不能把它交出去。
    #[test]
    fn refuses_files_outside_the_evidence_extensions() {
        let roots = vec![manifest()];
        let secrets = manifest().join("secrets.env");
        std::fs::write(&secrets, "GLM_API_KEY=不该被面板读到\n").unwrap();
        let got = resolve_allowed(secrets.to_str().unwrap(), &roots);
        std::fs::remove_file(&secrets).unwrap();
        let (code, _) = got.expect_err("密钥文件必须被拒");
        assert_eq!(code, StatusCode::FORBIDDEN);

        // Cargo.toml 也不是证据文件 —— 白名单之外一律不给。
        let p = manifest().join("Cargo.toml");
        assert_eq!(
            resolve_allowed(p.to_str().unwrap(), &roots).unwrap_err().0,
            StatusCode::FORBIDDEN
        );
    }

    #[test]
    fn refuses_anything_under_a_dot_directory() {
        let roots = vec![manifest()];
        let p = manifest().join(".git/config");
        if p.exists() {
            assert_eq!(
                resolve_allowed(p.to_str().unwrap(), &roots).unwrap_err().0,
                StatusCode::FORBIDDEN
            );
        }
    }

    #[test]
    fn rejects_traversal_out_of_the_root() {
        let roots = vec![manifest().join("contracts")];
        let escape = manifest().join("contracts/../evidence/M1_report.json");
        let (code, _) = resolve_allowed(escape.to_str().unwrap(), &roots)
            .expect_err("穿出白名单必须被拒");
        assert_eq!(code, StatusCode::FORBIDDEN);
    }

    #[test]
    fn rejects_absolute_path_outside_the_root() {
        let roots = vec![manifest()];
        let (code, _) = resolve_allowed("/etc/hosts", &roots).expect_err("白名单外必须被拒");
        assert_eq!(code, StatusCode::FORBIDDEN);
    }

    #[test]
    fn missing_file_is_not_found() {
        let roots = vec![manifest()];
        let p = manifest().join("绝对不存在的文件.json");
        let (code, _) = resolve_allowed(p.to_str().unwrap(), &roots).unwrap_err();
        assert_eq!(code, StatusCode::NOT_FOUND);
    }

    #[test]
    fn directory_is_not_served() {
        let roots = vec![manifest()];
        let p = manifest().join("contracts");
        let (code, _) = resolve_allowed(p.to_str().unwrap(), &roots).unwrap_err();
        assert_eq!(code, StatusCode::NOT_FOUND);
    }

    #[test]
    fn content_type_by_extension() {
        assert_eq!(
            content_type_for(Path::new("a.json")),
            "application/json; charset=utf-8"
        );
        assert_eq!(content_type_for(Path::new("a.png")), "image/png");
        assert_eq!(
            content_type_for(Path::new("snap.txt")),
            "text/plain; charset=utf-8"
        );
    }

    /// 极简 HTTP/1.1 客户端 —— 只为把服务真的跑起来问一句,
    /// 不值得为此拉一个 HTTP 客户端依赖进来。
    async fn get(addr: std::net::SocketAddr, path: &str) -> String {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let mut s = tokio::net::TcpStream::connect(addr).await.unwrap();
        s.write_all(format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n").as_bytes())
            .await
            .unwrap();
        let mut buf = Vec::new();
        s.read_to_end(&mut buf).await.unwrap();
        String::from_utf8_lossy(&buf).to_string()
    }

    /// 整条链路: 真起服务、真发请求、真收字节。
    #[tokio::test]
    async fn the_server_answers_over_real_http() {
        let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/viz");
        let paths = VizPaths {
            gol_root: fixtures.join("gol"),
            phonefarm_root: fixtures.join("phonefarm"),
            lock_root: fixtures.join("lock"),
            fleet: None,
        };
        let state = AppState {
            paths: Arc::new(paths),
        };
        // 端口 0 = 让内核挑一个空闲端口, 测试之间不会抢端口。
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, router(state)).await.unwrap();
        });

        let index = get(addr, "/").await;
        assert!(index.contains("200 OK"), "首页取不到: {}", &index[..index.len().min(120)]);
        assert!(index.contains("<html"));
        assert!(index.contains("no-store"), "面板要自刷新, 不该被缓存");

        let dash = get(addr, "/api/dashboard").await;
        assert!(dash.contains("200 OK"));
        assert!(dash.contains("\"layers\""));
        assert!(dash.contains("sysknob-loop"), "设备锁持有者该出现在返回里");

        assert!(get(addr, "/api/file?path=/etc/hosts").await.contains("403 Forbidden"));
        assert!(get(addr, "/healthz").await.ends_with("ok"));
    }

    #[test]
    fn embedded_assets_are_not_empty() {
        assert!(INDEX_HTML.contains("<html"));
        assert!(!APP_CSS.is_empty());
        assert!(APP_JS.contains("/api/dashboard"));
        // 离线可用的硬约束: 前端不许引外网资源。
        for asset in [INDEX_HTML, APP_CSS, APP_JS] {
            assert!(!asset.contains("http://"), "前端不该引外网资源");
            assert!(!asset.contains("https://"), "前端不该引外网资源");
        }
    }
}
