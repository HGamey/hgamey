// 实时看板的「节点暂时离线」兜底页（Cloudflare Worker）。
//
// 只挂在 phone.liyao.space/dash* 上（见 wrangler.toml）：/jobs、/mcp、/health 不经过它，
// agent 的轮询不计入 Worker 请求数，pf-node 自己的 5xx 重试也不受影响。
//
// 做的事只有一件：请求原样转给节点（流式，不缓存、不改头）；只有在 Cloudflare 自己因为
// 节点连不上而报错时（隧道断开的 1033 是 530，另有 502/503/504 的 Cloudflare 错误页，
// 或者 fetch 直接抛错），才换成一页中文的「节点暂时离线」。节点自己回的错误（JSON）照常透传。

const CF_ERROR_STATUS = new Set([502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530]);
const RETRY_S = 60;

export default {
  async fetch(request) {
    let resp;
    try {
      resp = await fetch(request);
    } catch (e) {
      return offline(request, "unreachable"); // 头里只能放 ASCII
    }
    if (isCloudflareError(resp)) return offline(request, String(resp.status));
    return resp;
  },
};

// 节点（dash）自己的错误都是 JSON；Cloudflare 的错误页是 HTML，且带 cf-error 相关特征。
function isCloudflareError(resp) {
  if (!CF_ERROR_STATUS.has(resp.status)) return false;
  if (resp.status === 530) return true; // 1033：隧道没连上
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("text/html");
}

function wantsHtml(request) {
  const url = new URL(request.url);
  if (url.pathname.startsWith("/dash/api/") || url.pathname.startsWith("/dash/static/")) return false;
  return request.method === "GET" && (request.headers.get("accept") || "").includes("text/html");
}

function offline(request, why) {
  const headers = { "Cache-Control": "no-store", "Retry-After": String(RETRY_S), "X-HG-Offline": why };
  if (!wantsHtml(request)) {
    return new Response(JSON.stringify({ error: "手机节点暂时离线，稍后再试", offline: true }), {
      status: 503,
      headers: { ...headers, "Content-Type": "application/json; charset=utf-8" },
    });
  }
  return new Response(PAGE, { status: 503, headers: { ...headers, "Content-Type": "text/html; charset=utf-8" } });
}

const PAGE = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="${RETRY_S}">
<title>HGamey 实时看板 · 暂时离线</title><link rel="icon" href="data:,">
<style>
:root { color-scheme: light; --page:#F5F7FA; --card:#FFFFFF; --ink:#0F2744; --ink2:#3D4B5F; --line:#D3DBE6; --blue:#1F5FBF; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --page:#0A1524; --card:#102038; --ink:#EEF2F8; --ink2:#C9D3E1; --line:#24405F; --blue:#5B93E6; } }
body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--page); color:var(--ink);
  font:16px/1.6 "PingFang SC","Helvetica Neue",system-ui,-apple-system,"Microsoft YaHei",sans-serif; }
.card { background:var(--card); border:1px solid var(--line); border-left:6px solid var(--blue); border-radius:14px; padding:24px 26px; width:min(520px, calc(100vw - 32px)); box-sizing:border-box; }
h1 { margin:0 0 6px; font-size:22px; } p { margin:6px 0; color:var(--ink2); } b { color:var(--ink); }
</style></head><body><main class="card">
<h1>手机节点暂时离线</h1>
<p>插着手机的那台电脑现在连不上（可能在重启、断网或断电）。<b>已经测出来的结果都存在那台电脑上</b>，它回来后看板上照样能看到。</p>
<p>这一页每 ${RETRY_S} 秒自动重试一次，恢复后会直接回到实时看板。</p>
</main></body></html>`;
