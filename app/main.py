"""FastAPI 入口：公告采集 API。

端点：
* GET  /                 演示页（可直接在浏览器发起采集）
* POST /api/v1/collect   传入列表页 URL 与项目编号，返回合并后的投标状态
* GET  /api/v1/collect   同上的查询参数形式，便于浏览器/curl 快速演示
* GET  /healthz          健康检查
"""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from app.collector import collect
from app.models import FetchRequest, FetchResponse

app = FastAPI(
    title="采购公告采集 API",
    description="抓取同站点原公告与更正公告，按发布时间合并，输出投标状态与有效截止时间。",
    version="1.0.0",
)


_DEMO_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>采购公告采集 API 演示</title>
<style>
body { font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 2rem; max-width: 960px; }
input { width: 32rem; max-width: 80vw; padding: .4rem; }
button { padding: .45rem 1.2rem; cursor: pointer; }
pre { background: #f4f4f6; padding: 1rem; border-radius: 8px; overflow:auto; max-height: 60vh; }
.hint { color: #666; font-size: .9rem; }
</style>
</head>
<body>
<h1>采购公告采集 API 演示</h1>
<p class="hint">示例站点由 docker compose 中的 nginx 提供，默认地址 <code>http://localhost:8080/list.html</code></p>
<form id="f">
  <p><label>列表页 URL <input id="list_url" value="http://localhost:8080/list.html"></label></p>
  <p><label>项目编号 <input id="project_code" value="GSB-2026-001"></label></p>
  <button type="submit">采集并合并</button>
</form>
<pre id="out">响应将显示在这里……</pre>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const out = document.getElementById('out');
  out.textContent = '请求中……';
  const payload = {
    list_url: document.getElementById('list_url').value,
    project_code: document.getElementById('project_code').value
  };
  try {
    const resp = await fetch('/api/v1/collect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    out.textContent = JSON.stringify(await resp.json(), null, 2);
  } catch (err) {
    out.textContent = '请求失败：' + err;
  }
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def demo_page() -> str:
    return _DEMO_PAGE


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_post(req: FetchRequest) -> FetchResponse:
    return await collect(req)


@app.get("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_get(
    list_url: str = Query(..., description="采购公告列表页 URL"),
    project_code: str = Query(..., description="项目编号"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> FetchResponse:
    return await collect(FetchRequest(list_url=list_url, project_code=project_code, timeout=timeout))
