"""报价核对编排与业务规则。

链路：
列表页发现 → 定位目标项目原公告 → 从原公告（及其同站页面）发现限价清单链接
→ 抓取并解析清单（还原跨行包号）→ 按包号过滤 → 与报价行逐项核对。

核对口径（对应需求）：
* 每行金额 = 清单数量 × 报价单价，**四舍五入到分**后再汇总为整包报价；
* 分项检查：报价单价 > 清单最高单价 → 超限；
* 整包检查：汇总报价 > 整包限价 → 超限；
* 缺报、重复编码、清单字段冲突（数量/单价/整包限价不一致或缺失）、
  关联页面读取失败 → 待核实（pending），不给出确定结论。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi import HTTPException

from app.collector import (
    build_http_client,
    discover_project,
    fetch_page,
)
from app.models import (
    NoticeKind,
    PackageCheckResult,
    QuoteCheckStatus,
    QuoteLineResult,
    QuoteRequest,
    QuoteResponse,
    SourceLink,
)
from app.price_parser import (
    PackageLimit,
    PriceItem,
    PriceParseError,
    PriceTableParseResult,
    extract_price_list_links,
    normalize_package,
    parse_price_tables,
    round_cents,
)

# 原因文案（同时用于行级与包级）
R_DUPLICATE_QUOTE = "报价中存在重复编码"
R_MISSING_ITEM = "清单有此项但报价缺报"
R_UNKNOWN_CODE = "报价编码不在限价清单中"
R_UNIT_OVER = "报价单价超过最高单价限价"
R_TOTAL_OVER = "整包报价超过整包限价"
R_QTY_CONFLICT = "清单数量字段冲突"
R_QTY_MISSING = "清单数量缺失"
R_UNIT_LIMIT_CONFLICT = "最高单价限价字段冲突"
R_UNIT_LIMIT_MISSING = "最高单价限价缺失"
R_PKG_LIMIT_CONFLICT = "整包限价字段冲突"
R_PKG_LIMIT_MISSING = "清单未提供整包限价"
R_FETCH_FAILED = "限价清单关联页面读取失败"
R_NO_PRICE_LIST = "原公告未链接可识别的限价清单"
R_PACKAGE_NOT_FOUND = "限价清单中未找到该包号"
R_CANNOT_AMOUNT = "数量缺失，无法计算本行金额"

_STATUS_RANK = {
    QuoteCheckStatus.OK: 0,
    QuoteCheckStatus.OVER_LIMIT: 1,
    QuoteCheckStatus.PENDING: 2,
    QuoteCheckStatus.NOT_FOUND: 3,
}


@dataclass
class _GatheredLists:
    """限价清单抓取与解析的汇总。"""

    items: list[PriceItem]
    package_limits: list[PackageLimit]
    sources: list[SourceLink]          # 成功的清单页
    failures: list[str]                # 抓取/解析失败的清单页 URL
    embedded_from: str | None          # 清单表格内嵌在哪个公告页（无独立清单页时）
    warnings: list[str]


async def _gather_price_lists(
    client: httpx.AsyncClient,
    list_url: str,
    project_code: str,
) -> tuple[_GatheredLists, list, list[str]]:
    """发现目标项目公告 → 抓取原公告链接的同站限价清单并解析。

    返回 ``(清单汇总, 目标项目公告列表, 公告页失败列表)``。
    """
    discovery = await discover_project(client, list_url, project_code)
    notices = discovery.notices

    # 1) 从原公告发现清单链接；原公告缺失时退而从全部项目公告页发现
    originals = [n for n in notices if n.kind == NoticeKind.ORIGINAL]
    discover_from = originals or notices

    link_map: dict[str, str] = {}  # 清单 URL -> 链接文本
    origin_pages: list[tuple[str, str]] = []  # 公告页 (url, html)，用于内嵌表格回退
    for notice in discover_from:
        url = str(notice.url)
        try:
            html = await fetch_page(client, url)
        except httpx.HTTPError:
            # 公告详情此前已成功抓取过，再次失败多为偶发；记录但不中断
            discovery.failures.append(url)
            continue
        origin_pages.append((url, html))
        for absolute, text, _ctx in extract_price_list_links(html, url):
            link_map.setdefault(absolute, text)

    semaphore = asyncio.Semaphore(5)
    gathered = _GatheredLists(items=[], package_limits=[], sources=[], failures=[],
                              embedded_from=None, warnings=[])

    async def fetch_and_parse(url: str) -> tuple[str, str | None, PriceTableParseResult | None]:
        async with semaphore:
            try:
                html = await fetch_page(client, url)
            except httpx.HTTPError:
                return url, None, None
            try:
                parsed = parse_price_tables(html, url)
            except PriceParseError:
                return url, html, None
            return url, html, parsed

    if link_map:
        results = await asyncio.gather(*(fetch_and_parse(u) for u in link_map))
        for url, html, parsed in results:
            if parsed is None:
                gathered.failures.append(url)
                continue
            gathered.items.extend(parsed.items)
            gathered.package_limits.extend(parsed.package_limits)
            gathered.warnings.extend(parsed.warnings)
            gathered.sources.append(
                SourceLink(role="price_list", url=url, title=link_map.get(url) or "限价清单")
            )

    # 2) 内嵌回退：没有独立清单链接，或独立清单页全部读取失败时，
    #    尝试直接解析原公告/公告页中的清单表格（部分平台把清单直接贴在公告正文里）
    if not link_map or not gathered.sources:
        notice_by_url = {str(n.url): n for n in discover_from}
        for url, html in origin_pages:
            try:
                parsed = parse_price_tables(html, url)
            except PriceParseError:
                continue
            gathered.items.extend(parsed.items)
            gathered.package_limits.extend(parsed.package_limits)
            gathered.warnings.extend(parsed.warnings)
            gathered.embedded_from = url
            notice = notice_by_url.get(url)
            gathered.sources.append(
                SourceLink(
                    role="price_list",
                    url=url,
                    title=f"{notice.title if notice else '公告页'}（内嵌限价清单）",
                )
            )

    return gathered, notices, sorted(set(discovery.failures))


def _field_conflict(values: list[Decimal]) -> tuple[bool, Decimal | None]:
    """同一编码的同一字段在清单中出现多个不一致取值 → 冲突；唯一值直接返回。"""
    distinct = {v for v in values if v is not None}
    if len(distinct) > 1:
        return True, None
    return False, (next(iter(distinct)) if distinct else None)


async def acheck_quote(req: QuoteRequest) -> QuoteResponse:
    list_url = str(req.list_url)
    package_no = normalize_package(req.package_no)

    try:
        async with build_http_client(req.timeout) as client:
            gathered, notices, notice_failures = await _gather_price_lists(
                client, list_url, req.project_code
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"列表页抓取失败：{list_url}（{exc.__class__.__name__}）",
        ) from exc

    warnings: list[str] = list(gathered.warnings)
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
            SourceLink(
                role=role_map[notice.kind],
                url=notice.url,
                title=notice.title,
            )
        )
    sources.extend(gathered.sources)
    for failed in notice_failures + gathered.failures:
        sources.append(SourceLink(role="failed", url=failed, title="读取失败-待核实"))

    has_original = any(n.kind == NoticeKind.ORIGINAL for n in notices)
    if not has_original or not notices:
        return _not_found(
            req, sources, warnings,
            reason="未找到该项目的原公告，无法定位限价清单",
        )

    # 清单页读取失败 → 整包待核实（仍输出已取得的明细，便于人工对照）
    fetch_failed = bool(gathered.failures or notice_failures)
    if fetch_failed:
        for url in gathered.failures:
            warnings.append(f"{R_FETCH_FAILED}：{url}")

    # 按包号过滤
    package_items = [i for i in gathered.items if i.package_no == package_no]
    package_limits = [p for p in gathered.package_limits if p.package_no == package_no]

    if not package_items and not package_limits:
        reason = R_PACKAGE_NOT_FOUND + f"：{req.package_no}"
        # 关联清单页读取失败时按需求优先标记“待核实”，而不是简单的未找到
        if fetch_failed:
            warnings.append(reason)
            return _pending_from_fetch_failure(req, sources, warnings)
        warnings.append(reason)
        return _not_found(req, sources, warnings, reason=R_PACKAGE_NOT_FOUND)

    if not gathered.sources:
        warnings.append(R_NO_PRICE_LIST)
        return _not_found(req, sources, warnings, reason=R_NO_PRICE_LIST)

    # ------------------------------------------------------------------ #
    # 清单侧：按编码归并，检测字段冲突
    # ------------------------------------------------------------------ #
    grouped: dict[str, list[PriceItem]] = {}
    for item in package_items:
        grouped.setdefault(item.item_code, []).append(item)

    @dataclass
    class CatalogEntry:
        code: str
        name: str | None
        quantity: Decimal | None
        unit: str | None
        limit_unit_price: Decimal | None
        source_url: str
        qty_conflict: bool
        unit_price_conflict: bool

    catalog: dict[str, CatalogEntry] = {}
    for code, entries in grouped.items():
        qty_conflict, quantity = _field_conflict([e.quantity for e in entries])
        up_conflict, unit_price = _field_conflict([e.limit_unit_price for e in entries])
        names = {e.item_name for e in entries if e.item_name}
        catalog[code] = CatalogEntry(
            code=code,
            name=next(iter(names)) if names else None,
            quantity=quantity,
            unit=next((e.unit for e in entries if e.unit), None),
            limit_unit_price=unit_price,
            source_url=entries[0].source_url,
            qty_conflict=qty_conflict,
            unit_price_conflict=up_conflict,
        )

    # 整包限价冲突
    pkg_amounts = Counter(p.amount for p in package_limits)
    pkg_conflict = len(pkg_amounts) > 1
    if pkg_conflict:
        warnings.append(
            R_PKG_LIMIT_CONFLICT
            + f"：发现 {len(pkg_amounts)} 个不一致取值（{_format_amounts(pkg_amounts.keys())}）"
        )
    package_limit: Decimal | None = (
        pkg_amounts.most_common(1)[0][0] if len(pkg_amounts) == 1 else None
    )

    # 报价侧重复编码
    quote_counter = Counter(line.item_code.strip() for line in req.lines)
    duplicate_codes = {code for code, count in quote_counter.items() if count > 1}

    line_results: list[QuoteLineResult] = []
    overall = QuoteCheckStatus.OK
    package_reasons: list[str] = []
    unpriced_codes: list[str] = []
    if fetch_failed:
        overall = QuoteCheckStatus.PENDING
        package_reasons.append(R_FETCH_FAILED)
    if duplicate_codes:
        package_reasons.append(f"{R_DUPLICATE_QUOTE}：{sorted(duplicate_codes)}")
    if pkg_conflict:
        package_reasons.append(R_PKG_LIMIT_CONFLICT)

    def escalate(target: QuoteCheckStatus) -> None:
        nonlocal overall
        if _STATUS_RANK[target] > _STATUS_RANK[overall]:
            overall = target

    # ------------------------------------------------------------------ #
    # 逐项核对（以清单编码为主序，保证缺报项可被识别）
    # ------------------------------------------------------------------ #
    quoted_map: dict[str, list[Decimal]] = {}
    for line in req.lines:
        quoted_map.setdefault(line.item_code.strip(), []).append(line.unit_price)

    computed_total = Decimal("0")
    total_computable = True

    for code in sorted(catalog):
        entry = catalog[code]
        reasons: list[str] = []
        status = QuoteCheckStatus.OK
        quoted_prices = quoted_map.get(code, [])
        quoted_price = quoted_prices[0] if quoted_prices else None

        if entry.qty_conflict:
            reasons.append(R_QTY_CONFLICT)
        elif entry.quantity is None:
            reasons.append(R_QTY_MISSING)
        if entry.unit_price_conflict:
            reasons.append(R_UNIT_LIMIT_CONFLICT)
        elif entry.limit_unit_price is None:
            reasons.append(R_UNIT_LIMIT_MISSING)
        if code in duplicate_codes:
            reasons.append(R_DUPLICATE_QUOTE)

        # 金额计算
        amount: Decimal | None = None
        if quoted_price is None:
            reasons.append(R_MISSING_ITEM)
            total_computable = False
            unpriced_codes.append(code)
        elif entry.quantity is None:
            reasons.append(R_CANNOT_AMOUNT)
            total_computable = False
            unpriced_codes.append(code)
        else:
            amount = round_cents(entry.quantity * quoted_price)
            computed_total += amount

        # 分项单价检查
        if quoted_price is not None and entry.limit_unit_price is not None:
            if quoted_price > entry.limit_unit_price:
                reasons.append(
                    f"{R_UNIT_OVER}：报价 {quoted_price} > 限价 {entry.limit_unit_price}"
                )

        if any(
            r in (R_QTY_CONFLICT, R_QTY_MISSING, R_UNIT_LIMIT_CONFLICT,
                  R_UNIT_LIMIT_MISSING, R_CANNOT_AMOUNT)
            for r in reasons
        ) or code in duplicate_codes or quoted_price is None:
            status = QuoteCheckStatus.PENDING
        elif any(R_UNIT_OVER in r for r in reasons):
            status = QuoteCheckStatus.OVER_LIMIT
        escalate(status)

        line_results.append(
            QuoteLineResult(
                item_code=code,
                item_name=entry.name,
                quantity=entry.quantity,
                unit=entry.unit,
                limit_unit_price=entry.limit_unit_price,
                quoted_unit_price=quoted_price,
                quoted_amount=amount,
                status=status,
                reasons=reasons,
                source_url=entry.source_url,
            )
        )

    # 报价中存在、清单中不存在的编码
    for code in sorted(quoted_map):
        if code in catalog:
            continue
        prices = quoted_map[code]
        reasons = [R_UNKNOWN_CODE]
        if code in duplicate_codes:
            reasons.append(R_DUPLICATE_QUOTE)
        line_results.append(
            QuoteLineResult(
                item_code=code,
                item_name=None,
                quantity=None,
                unit=None,
                limit_unit_price=None,
                quoted_unit_price=prices[0],
                quoted_amount=None,
                status=QuoteCheckStatus.PENDING,
                reasons=reasons,
                source_url=None,
            )
        )
        escalate(QuoteCheckStatus.PENDING)
        package_reasons.append(f"{R_UNKNOWN_CODE}：{code}")
        total_computable = False
        unpriced_codes.append(code)

    # 清单内部数量/单价冲突或缺失升级为包级待核实
    if any(e.qty_conflict or e.unit_price_conflict
           or e.quantity is None or e.limit_unit_price is None for e in catalog.values()):
        escalate(QuoteCheckStatus.PENDING)
        if any(e.qty_conflict for e in catalog.values()):
            package_reasons.append(R_QTY_CONFLICT)
        if any(e.quantity is None and not e.qty_conflict for e in catalog.values()):
            package_reasons.append(R_QTY_MISSING)
        if any(e.unit_price_conflict for e in catalog.values()):
            package_reasons.append(R_UNIT_LIMIT_CONFLICT)
        if any(e.limit_unit_price is None and not e.unit_price_conflict
               for e in catalog.values()):
            package_reasons.append(R_UNIT_LIMIT_MISSING)

    # ------------------------------------------------------------------ #
    # 整包检查
    # ------------------------------------------------------------------ #
    quoted_total = round_cents(computed_total) if total_computable else None
    if not total_computable:
        package_reasons.append(
            "存在缺报/无法计价项（" + "、".join(unpriced_codes) + "），整包金额未能完整核算，需人工核实"
        )
        escalate(QuoteCheckStatus.PENDING)

    if package_limit is None:
        if package_limits:  # 多值冲突场景
            package_reasons.append(R_PKG_LIMIT_CONFLICT)
        else:
            package_reasons.append(R_PKG_LIMIT_MISSING)
            warnings.append(R_PKG_LIMIT_MISSING + "，仅完成分项核对")
        escalate(QuoteCheckStatus.PENDING)
    elif quoted_total is not None and quoted_total > package_limit:
        package_reasons.append(
            f"{R_TOTAL_OVER}：整包报价 {quoted_total} > 整包限价 {package_limit}"
        )
        escalate(QuoteCheckStatus.OVER_LIMIT)

    package_result = PackageCheckResult(
        package_no=req.package_no,
        status=overall,
        quoted_total=quoted_total,
        package_limit=package_limit,
        reasons=_dedup(package_reasons),
    )

    return QuoteResponse(
        project_code=req.project_code,
        package_no=req.package_no,
        list_url=req.list_url,
        status=overall,
        package=package_result,
        line_results=line_results,
        sources=_dedup_sources(sources),
        warnings=_dedup(warnings),
        fetched_at=datetime.now(timezone.utc),
    )


def _format_amounts(amounts) -> str:
    return "、".join(str(a) for a in amounts)


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _dedup_sources(sources: list[SourceLink]) -> list[SourceLink]:
    seen: set[tuple[str, str]] = set()
    out: list[SourceLink] = []
    for source in sources:
        key = (source.role, str(source.url))
        if key not in seen:
            seen.add(key)
            out.append(source)
    return out


def _not_found(
    req: QuoteRequest,
    sources: list[SourceLink],
    warnings: list[str],
    reason: str,
) -> QuoteResponse:
    warnings = _dedup(warnings + [reason])
    return QuoteResponse(
        project_code=req.project_code,
        package_no=req.package_no,
        list_url=req.list_url,
        status=QuoteCheckStatus.NOT_FOUND,
        package=PackageCheckResult(
            package_no=req.package_no,
            status=QuoteCheckStatus.NOT_FOUND,
            quoted_total=None,
            package_limit=None,
            reasons=[reason],
        ),
        line_results=[],
        sources=_dedup_sources(sources),
        warnings=warnings,
        fetched_at=datetime.now(timezone.utc),
    )


def _pending_from_fetch_failure(
    req: QuoteRequest,
    sources: list[SourceLink],
    warnings: list[str],
) -> QuoteResponse:
    """关联限价清单页读取失败且无可用清单数据 → 整包待核实。"""
    return QuoteResponse(
        project_code=req.project_code,
        package_no=req.package_no,
        list_url=req.list_url,
        status=QuoteCheckStatus.PENDING,
        package=PackageCheckResult(
            package_no=req.package_no,
            status=QuoteCheckStatus.PENDING,
            quoted_total=None,
            package_limit=None,
            reasons=_dedup([R_FETCH_FAILED, R_PACKAGE_NOT_FOUND]),
        ),
        line_results=[],
        sources=_dedup_sources(sources),
        warnings=_dedup(warnings),
        fetched_at=datetime.now(timezone.utc),
    )
