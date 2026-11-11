# 采购公告采集与报价核对 API

提供两组能力：

1. **公告采集**：传入**采购公告列表页 URL** 与**项目编号**，自动抓取该页指向的同站点原公告与更正公告，
   从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后返回：
   当前是否可投标、有效截止时间、项目状态以及全部来源链接。
2. **报价核对**：传入列表页 URL、项目编号、包号及报价行（清单项编码 + 单价），
   自动定位该项目**原公告链接的同站 HTML 限价清单**，还原合并单元格跨行填写的包号，
   解析各项编码、数量、最高单价与整包限价；按“清单数量 × 报价单价、四舍五入到分后汇总”的口径，
   分别检查**分项单价**与**整包总价**是否超过公告限价，缺报/重复/冲突/读取失败时标记**待核实**。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 入口、路由、浏览器演示页（采集 + 报价核对两个 Tab）
│   ├── collector.py     # 采集编排与共享发现逻辑：列表发现 → 并发抓取 → 归属判定 → 合并
│   ├── parser.py        # 公告 HTML 解析：同站链接发现、三层字段提取、取消识别
│   ├── price_parser.py  # 限价清单解析：rowspan/colspan 还原、表头映射、金额标准化
│   ├── merger.py        # 业务合并规则（时间继承、延期覆盖、取消、待核实）
│   ├── quote.py         # 报价核对编排与规则（金额计算、超限/冲突/缺报判定、来源追溯）
│   └── models.py        # 请求/响应 Pydantic 模型
├── sample-site/         # 本地示例公告站点（nginx 静态托管，可完全离线使用）
│   ├── list.html        # 公告列表页（含外站/登录干扰链接）
│   └── detail/          # 3 个项目的原公告/更正/取消详情页 + 限价清单样例
├── nginx/default.conf   # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/               # 解析/合并/报价单测 + 本地静态服务器端到端测试（73 个用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 两个 Tab：填表即可发起采集或报价核对 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |

停止：`docker compose down`。

## API 用法

### POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

## 示例站点内置的三个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 报价核对 API（`POST /api/v1/quote-check`）

### 请求

| 字段 | 说明 |
| --- | --- |
| `list_url` | 采购公告列表页 URL（与采集接口一致） |
| `project_code` | 项目编号，如 `GSB-2026-001` |
| `package_no` | 包号，支持 `1` / `第1包` / `包1` 等写法（自动规范化） |
| `lines` | 报价行数组：`item_code`（清单项编码）+ `unit_price`（报价单价，元） |
| `timeout` | 单次请求超时秒数，默认 10 |

```bash
curl -X POST http://localhost:8000/api/v1/quote-check \
  -H 'Content-Type: application/json' \
  -d '{
    "list_url": "http://localhost:8080/list.html",
    "project_code": "GSB-2026-001",
    "package_no": "2",
    "lines": [
      {"item_code": "YM-101", "unit_price": "260.00"},
      {"item_code": "YM-102", "unit_price": "16.25"}
    ]
  }'
```

### 核对口径与状态

- **逐项金额**：`本行金额 = 清单数量 × 报价单价`，使用 `Decimal` 精确计算并按
  `ROUND_HALF_UP` **四舍五入到分**；整包报价为各行金额之和。
- **分项检查**：报价单价 > 清单“最高单价限价” → 该行 `over_limit`。
- **整包检查**：整包报价 > 整包限价 → 包级 `over_limit`；等于限价不算超限。
- `status` 取值：`ok`（未超限）/ `over_limit`（明确超限）/ `pending`（待核实）/ `not_found`（未找到公告或包号）。

下列情况标记 **`pending` 待核实**，不输出确定的超限结论：

| 场景 | 处理 |
| --- | --- |
| 清单有项但报价**缺报** | 该行 `quoted_unit_price=null`，整包报价置 `null` |
| 报价中**重复编码** | 对应行与包级均标注重复原因 |
| 报价编码**不在清单** | 单列结果行，原因“报价编码不在限价清单中” |
| 清单同一编码的**数量/最高单价跨表或跨页不一致** | 字段冲突，冲突值置 `null` |
| **整包限价**多值冲突或清单未提供 | `package_limit=null`，仅完成可判定的分项核对 |
| 限价清单**关联页面读取失败**（404/超时/结构异常） | 包级待核实，失败 URL 进入 `sources(role=failed)`；若公告正文内嵌清单表格则回退解析并照常输出分项明细 |

### 响应关键字段

```json
{
  "project_code": "GSB-2026-001",
  "package_no": "2",
  "status": "ok",
  "package": {
    "package_no": "2",
    "status": "ok",
    "quoted_total": "52000.00",
    "package_limit": "52000.00",
    "reasons": []
  },
  "line_results": [
    {
      "item_code": "YM-101",
      "item_name": "行道树补植（含支撑架）",
      "quantity": "150",
      "unit": "株",
      "limit_unit_price": "260.00",
      "quoted_unit_price": "260.00",
      "quoted_amount": "39000.00",
      "status": "ok",
      "reasons": [],
      "source_url": "http://localhost:8080/detail/g001-price-2.html"
    }
  ],
  "sources": [
    {"role": "list", "url": "..."},
    {"role": "original", "url": "..."},
    {"role": "price_list", "url": ".../g001-price-2.html"}
  ],
  "warnings": []
}
```

### 限价清单解析能力（`app/price_parser.py`）

- **合并单元格还原**：把 `rowspan`/`colspan` 展开为规则网格，包号跨行只写一次时自动继承到每个明细项；
- **表头识别**：按“包号/编码/名称/单位/数量/最高单价/整包限价”等中文关键词映射列，兼容不同叫法；
- **整包限价**：支持独立“包预算汇总表”，也支持明细表内的整包限价列（可跨行合并）；
- **金额格式**：支持千分位、`￥/元` 后缀与中文大写金额（如 `柒万陆仟柒佰伍拾元整`）；
- **链接发现**：仅跟踪原公告指向的**同站点**、且 href/文本含“限价/清单/预算/采购需求/工程量/明细”语义的链接，
  登录/导航/外站链接自动忽略；没有独立清单页时回退解析公告正文内嵌表格。

### 样例站点内置的限价清单（离线可用）

| 页面 | 场景 |
| --- | --- |
| `detail/g001-price-1.html` | 第1包：包号 `rowspan` 跨行 + 独立整包限价汇总表（含中文大写金额） |
| `detail/g001-price-2.html` | 第2包：明细表内整包限价列跨行合并 |
| `detail/g001-price-conflict.html` | 第1包第二来源：与首份清单数量/单价/整包限价不一致，演练**字段冲突 → 待核实** |
| `detail/g003-price-missing.html`（不存在） | 链接保留但页面 404，演练**关联页面读取失败 → 待核实** |
| `detail/g003-price-embedded.html` | 公告直接链接的内嵌清单备份页 |

以上页面均由 docker compose 中的 `sample-site` 容器以 nginx 静态文件提供，
**整套报价核对链路无需访问外网即可离线调用**。



```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖）
pytest -q
```

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
