"""采集编排：列表页发现 → 详情页抓取 → 字段解析 → 业务合并。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from app.merger import MergeInput, merge_notices
from app.models import (
    FetchRequest,
    FetchResponse,
    NoticeKind,
    NoticeRecord,
    ProjectStatus,
    SourceLink,
)
from app.parser import ParseError, extract_detail_links, parse_detail

_MAX_CONCURRENCY = 5
_USER_AGENT = "TenderCollector/1.0 (+local procurement announcement API)"
# 项目编号匹配时容忍大小写/空格/分隔符差异，如 “GSB-2026-001” 与 “GSB 2026 001”
_CODE_NORM = str.maketrans({" ": "", "　": "", "_": "", "/": "", "\\": "", "-": ""})


def build_http_client(timeout: float) -> httpx.AsyncClient:
    """统一的 HTTP 客户端：自定义 UA、跟随重定向、统一超时。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )


def normalize_code(code: str) -> str:
    return code.strip().lower().translate(_CODE_NORM)


def _code_matches(page_code: str | None, requested: str, title: str, row_context: str) -> bool:
    target = normalize_code(requested)
    if page_code and normalize_code(page_code) == target:
        return True
    # 详情页没有结构化编号时，退化为标题/列表行上下文包含匹配
    haystack = normalize_code(f"{title} {row_context}")
    return target in haystack


async def fetch_page(client: httpx.AsyncClient, url: str) -> str:
    """抓取页面文本；4xx/5xx（如下线详情页 404）一律抛出，由调用方归类为失败。"""
    response = await client.get(url)
    response.raise_for_status()
    return response.text


@dataclass
class ProjectDiscovery:
    """列表页发现 + 同站详情抓取的中间结果，供采集与报价核对复用。"""

    notices: list[NoticeRecord] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # 属于目标项目但抓取/解析失败的详情页
    list_html: str = ""


async def discover_project(
    client: httpx.AsyncClient, list_url: str, project_code: str
) -> ProjectDiscovery:
    """抓取列表页并并发抓取同站候选详情页，筛出属于目标项目的公告。

    列表页本身抓取失败由调用方（端点编排）转为 502；
    目标项目相关详情页的失败不抛异常，沉淀到 ``failures`` 中按待核实处理。
    """
    list_html = await fetch_page(client, list_url)
    candidates = extract_detail_links(list_html, list_url)
    if not candidates:
        return ProjectDiscovery(notices=[], failures=[], list_html=list_html)

    target_code = normalize_code(project_code)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def fetch_one(url: str, link_text: str, context: str) -> tuple[str, str, str, str]:
        async with semaphore:
            try:
                html = await fetch_page(client, url)
                return url, html, link_text, context
            except httpx.HTTPError:
                return url, "", link_text, context  # 空 html 表示失败

    results = await asyncio.gather(*(fetch_one(u, t, c) for u, t, c in candidates))

    notices: list[NoticeRecord] = []
    failures: list[str] = []
    for url, html, _link_text, context in results:
        # 列表行上下文包含目标项目编号 → 该链接属于本项目（即使详情页抓不回来）
        row_mentions_project = target_code in normalize_code(context)

        if not html:
            if row_mentions_project:
                failures.append(url)
            continue
        try:
            data = parse_detail(html, url)
        except ParseError:
            if row_mentions_project:
                failures.append(url)
            continue

        if not _code_matches(data.project_code, project_code, data.title, context):
            continue  # 同站点但属于其他项目，忽略

        kind = {
            "original": NoticeKind.ORIGINAL,
            "correction": NoticeKind.CORRECTION,
            "cancellation": NoticeKind.CANCELLATION,
        }[data.kind_hint]
        notices.append(
            NoticeRecord(
                url=url,
                kind=kind,
                title=data.title,
                published_at=data.published_at,
                deadline=data.deadline,
                cancelled=data.cancelled,
                cancellation_reason=data.cancellation_reason,
            )
        )
        # 缺发布时间在 merger 中按待核实处理。

    return ProjectDiscovery(
        notices=notices,
        failures=sorted(set(failures)),
        list_html=list_html,
    )


def build_sources(
    list_url: str,
    notices: list[NoticeRecord],
    failures: list[str],
    extra: list[SourceLink] | None = None,
) -> list[SourceLink]:
    """组装来源链接：列表页 → 公告明细 → 失败页面，再追加额外来源（如限价清单）。"""
    sources: list[SourceLink] = [
        SourceLink(role="list", url=list_url, title="采购公告列表页")
    ]
    role_map = {
        NoticeKind.ORIGINAL: "original",
        NoticeKind.CORRECTION: "correction",
        NoticeKind.CANCELLATION: "cancellation",
    }
    for notice in notices:
        sources.append(
            SourceLink(role=role_map[notice.kind], url=notice.url, title=notice.title)
        )
    for failed_url in failures:
        sources.append(SourceLink(role="failed", url=failed_url, title="抓取失败-待核实"))
    if extra:
        sources.extend(extra)
    return sources


async def collect(req: FetchRequest) -> FetchResponse:
    list_url = str(req.list_url)
    try:
        async with build_http_client(req.timeout) as client:
            discovery = await discover_project(client, list_url, req.project_code)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"列表页抓取失败：{list_url}（{exc.__class__.__name__}）",
        ) from exc

    if not discovery.notices and not discovery.failures:
        return _empty_response(req, discovery)

    merge_result = merge_notices(
        MergeInput(notices=discovery.notices, failures=discovery.failures)
    )

    return FetchResponse(
        project_code=req.project_code,
        list_url=req.list_url,
        status=merge_result.status,
        can_bid=merge_result.can_bid,
        effective_deadline=merge_result.effective_deadline,
        cancelled=merge_result.cancelled,
        notices=merge_result.ordered_notices,
        sources=build_sources(list_url, merge_result.ordered_notices, discovery.failures),
        warnings=merge_result.warnings,
        fetched_at=datetime.now(timezone.utc),
    )


def _empty_response(req: FetchRequest, discovery: ProjectDiscovery | None = None) -> FetchResponse:
    if discovery is not None and discovery.list_html:
        warnings = ["列表页未发现与该项目编号相关的同站点公告"]
    else:
        warnings = ["列表页未发现任何同站点详情链接"]
    return FetchResponse(
        project_code=req.project_code,
        list_url=req.list_url,
        status=ProjectStatus.NOT_FOUND,
        can_bid=False,
        effective_deadline=None,
        cancelled=False,
        notices=[],
        sources=[
            SourceLink(role="list", url=str(req.list_url), title="采购公告列表页")
        ],
        warnings=warnings,
        fetched_at=datetime.now(timezone.utc),
    )
