"""Pydantic 数据模型：请求 / 响应 / 内部记录。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class ProjectStatus(str, Enum):
    """投标状态。

    OPEN         可投标（存在有效截止时间，且未取消）
    CLOSED       不可投标（公告明确取消采购）
    PENDING      待核实（抓取/解析失败，或无法得到有效截止时间，不能把旧时间当最终期限）
    NOT_FOUND    列表页未发现与该项目编号相关的同站点公告
    """

    OPEN = "open"
    CLOSED = "closed"
    PENDING = "pending"
    NOT_FOUND = "not_found"


class NoticeKind(str, Enum):
    ORIGINAL = "original"          # 原公告
    CORRECTION = "correction"      # 更正公告（含澄清、变更、延期）
    CANCELLATION = "cancellation"  # 取消公告（更正的一种，且明确取消采购）


class QuoteCheckStatus(str, Enum):
    """报价核对结论。

    OK           未超限
    OVER_LIMIT   明确超过限价（分项单价或整包总价）
    PENDING      待核实（缺报、重复编码、字段冲突、关联页面读取失败等，不能给出确定结论）
    NOT_FOUND    未找到该项目的原公告或对应包号的限价清单
    """

    OK = "ok"
    OVER_LIMIT = "over_limit"
    PENDING = "pending"
    NOT_FOUND = "not_found"


class FetchRequest(BaseModel):
    """采集请求。"""

    list_url: HttpUrl = Field(..., description="采购公告列表页 URL")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-001")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class NoticeRecord(BaseModel):
    """单条公告详情的解析结果（按发布时间排序）。"""

    url: HttpUrl
    kind: NoticeKind
    title: str
    published_at: datetime | None = None
    deadline: datetime | None = Field(
        None, description="该公告声明的投标截止时间；未提及则为 None，合并时沿用已生效时间"
    )
    cancelled: bool = False
    cancellation_reason: str | None = None


class SourceLink(BaseModel):
    """来源链接（列表页 + 参与合并的详情页）。"""

    role: str  # list / original / correction / cancellation / price_list / failed
    url: HttpUrl
    title: str | None = None


class FetchResponse(BaseModel):
    """采集响应。"""

    project_code: str
    list_url: HttpUrl
    status: ProjectStatus
    can_bid: bool = Field(..., description="当前是否可投标")
    effective_deadline: datetime | None = Field(
        None, description="有效投标截止时间；待核实/未找到时为 None，绝不返回旧时间冒充最终期限"
    )
    cancelled: bool = False
    notices: list[NoticeRecord] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime


# --------------------------------------------------------------------------- #
# 报价核对
# --------------------------------------------------------------------------- #
class QuoteLine(BaseModel):
    """投标企业填报的一行报价。"""

    item_code: str = Field(..., min_length=1, description="清单项编码")
    unit_price: Decimal = Field(..., ge=0, description="报价单价（元）")


class QuoteRequest(BaseModel):
    """报价核对请求。"""

    list_url: HttpUrl = Field(..., description="采购公告列表页 URL")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-001")
    package_no: str = Field(..., min_length=1, description="包号，如 1 / 第1包 / 包1")
    lines: list[QuoteLine] = Field(..., min_length=1, description="报价行：清单项编码 + 报价单价")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class QuoteLineResult(BaseModel):
    """逐项核对结果。清单中存在但报价缺报的项也会返回（quoted_unit_price 为 null）。"""

    item_code: str
    item_name: str | None = None
    quantity: Decimal | None = Field(None, description="清单数量")
    unit: str | None = Field(None, description="计量单位")
    limit_unit_price: Decimal | None = Field(None, description="清单最高单价限价（元）")
    quoted_unit_price: Decimal | None = Field(None, description="报价单价（元）；缺报时为 null")
    quoted_amount: Decimal | None = Field(
        None, description="本行金额 = 清单数量 × 报价单价，四舍五入到分（元）；无法计算时为 null"
    )
    status: QuoteCheckStatus
    reasons: list[str] = Field(default_factory=list, description="超限/待核实原因")
    source_url: HttpUrl | None = Field(None, description="该清单项的来源页面")


class PackageCheckResult(BaseModel):
    """整包核对结果。"""

    package_no: str
    status: QuoteCheckStatus
    quoted_total: Decimal | None = Field(None, description="报价汇总（各行金额四舍五入到分后求和，元）")
    package_limit: Decimal | None = Field(None, description="整包限价（元）；缺失或冲突时为 null")
    reasons: list[str] = Field(default_factory=list)


class QuoteResponse(BaseModel):
    """报价核对响应。"""

    project_code: str
    package_no: str
    list_url: HttpUrl
    status: QuoteCheckStatus = Field(..., description="整包总体结论")
    package: PackageCheckResult
    line_results: list[QuoteLineResult] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime
