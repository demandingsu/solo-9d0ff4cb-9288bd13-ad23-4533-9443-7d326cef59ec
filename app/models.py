"""Pydantic 数据模型：请求 / 响应 / 内部记录。"""

from __future__ import annotations

from datetime import datetime
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

    role: str  # list / original / correction / cancellation
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
