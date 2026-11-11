"""FastAPI 入口：公告采集 API + 报价核对 API。

端点：
* GET  /                        演示页（可直接在浏览器发起采集/报价核对）
* POST /api/v1/collect          传入列表页 URL 与项目编号，返回合并后的投标状态
* GET  /api/v1/collect          同上的查询参数形式，便于浏览器/curl 快速演示
* POST /api/v1/quote-check      报价核对：限价清单解析 + 分项/整包超限检查
* GET  /healthz                 健康检查
"""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from app.collector import collect
from app.models import FetchRequest, FetchResponse, QuoteRequest, QuoteResponse
from app.quote import acheck_quote

app = FastAPI(
    title="采购公告采集与报价核对 API",
    description=(
        "抓取同站点原公告与更正公告，按发布时间合并，输出投标状态与有效截止时间；"
        "并支持解析同站限价清单（含合并单元格包号还原），核对报价单价与整包总价是否超限。"
    ),
    version="1.1.0",
)


_DEMO_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>采购公告采集 / 报价核对 API 演示</title>
<style>
body { font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 2rem; max-width: 1080px; }
input { width: 32rem; max-width: 80vw; padding: .4rem; }
button { padding: .45rem 1.2rem; cursor: pointer; }
pre { background: #f4f4f6; padding: 1rem; border-radius: 8px; overflow:auto; max-height: 60vh; }
.hint { color: #666; font-size: .9rem; }
.tabs { display:flex; gap:.5rem; margin-bottom:1rem; }
.tabs button { border:1px solid #bbb; background:#eee; border-radius:6px 6px 0 0; }
.tabs button.active { background:#1a5fb4; color:#fff; }
.panel { display:none; border:1px solid #ddd; border-radius:0 8px 8px 8px; padding:1.2rem; }
.panel.active { display:block; }
table.q { border-collapse:collapse; }
table.q td, table.q th { border:1px solid #ccc; padding:.35rem .6rem; }
table.q input { width: 12rem; }
</style>
</head>
<body>
<h1>采购公告采集 / 报价核对 API 演示</h1>
<p class="hint">示例站点由 docker compose 中的 nginx 提供，默认地址 <code>http://localhost:8080/list.html</code></p>
<div class="tabs">
  <button id="tab-collect" class="active" type="button">公告采集</button>
  <button id="tab-quote" type="button">报价核对</button>
</div>

<section id="panel-collect" class="panel active">
<form id="f-collect">
  <p><label>列表页 URL <input id="c_list_url" value="http://localhost:8080/list.html"></label></p>
  <p><label>项目编号 <input id="c_project_code" value="GSB-2026-001"></label></p>
  <button type="submit">采集并合并</button>
</form>
<pre id="c_out">响应将显示在这里……</pre>
</section>

<section id="panel-quote" class="panel">
<form id="f-quote">
  <p><label>列表页 URL <input id="q_list_url" value="http://localhost:8080/list.html"></label></p>
  <p><label>项目编号 <input id="q_project_code" value="GSB-2026-001"></label></p>
  <p><label>包号 <input id="q_package_no" value="1" style="width:8rem"></label></p>
  <p>报价行（清单项编码 + 单价）：</p>
  <table class="q" id="q_lines">
    <tr><th>清单项编码</th><th>报价单价（元）</th><th></th></tr>
  </table>
  <p><button type="button" id="q_add">+ 增加一行</button></p>
  <button type="submit">核对报价</button>
</form>
<pre id="q_out">响应将显示在这里……</pre>
</section>

<script>
document.getElementById('tab-collect').addEventListener('click', () => switchTab('collect'));
document.getElementById('tab-quote').addEventListener('click', () => switchTab('quote'));
function switchTab(name) {
  for (const n of ['collect','quote']) {
    document.getElementById('tab-'+n).classList.toggle('active', n===name);
    document.getElementById('panel-'+n).classList.toggle('active', n===name);
  }
}
function addLine(code='', price='') {
  const tr = document.createElement('tr');
  tr.innerHTML = '<td><input class="q-code" value="'+code+'"></td>'
    + '<td><input class="q-price" value="'+price+'"></td>'
    + '<td><button type="button" class="q-del">删除</button></td>';
  document.getElementById('q_lines').appendChild(tr);
  tr.querySelector('.q-del').addEventListener('click', () => tr.remove());
}
addLine('YL-001', '12.50');
addLine('YL-002', '305.00');
addLine('YL-003', '8.80');
document.getElementById('q_add').addEventListener('click', () => addLine());

document.getElementById('f-collect').addEventListener('submit', async (e) => {
  e.preventDefault();
  const out = document.getElementById('c_out');
  out.textContent = '请求中……';
  try {
    const resp = await fetch('/api/v1/collect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        list_url: document.getElementById('c_list_url').value,
        project_code: document.getElementById('c_project_code').value
      })
    });
    out.textContent = JSON.stringify(await resp.json(), null, 2);
  } catch (err) { out.textContent = '请求失败：' + err; }
});

document.getElementById('f-quote').addEventListener('submit', async (e) => {
  e.preventDefault();
  const out = document.getElementById('q_out');
  out.textContent = '请求中……';
  const lines = [...document.querySelectorAll('#q_lines tr')].slice(1).map(tr => ({
    item_code: tr.querySelector('.q-code').value.trim(),
    unit_price: tr.querySelector('.q-price').value.trim()
  })).filter(l => l.item_code && l.unit_price !== '');
  try {
    const resp = await fetch('/api/v1/quote-check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        list_url: document.getElementById('q_list_url').value,
        project_code: document.getElementById('q_project_code').value,
        package_no: document.getElementById('q_package_no').value,
        lines
      })
    });
    out.textContent = JSON.stringify(await resp.json(), null, 2);
  } catch (err) { out.textContent = '请求失败：' + err; }
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


@app.post("/api/v1/quote-check", response_model=QuoteResponse, tags=["quote-check"])
async def quote_check_post(req: QuoteRequest) -> QuoteResponse:
    return await acheck_quote(req)
