"""HTML 解析：列表页链接发现 + 详情页字段提取。

解析策略（三层，逐层回退）：
1. 结构化标记：``data-field`` / ``meta[property]``，演示站点和现代采购平台可直接使用；
2. 标签匹配：按“发布时间/截止时间/取消”等中文标签定位文本节点；
3. 时间正则：在正文文本中兜底识别日期时间，并排除发布时间本身。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# 支持的日期时间写法，如 2026-11-05 09:00、2026年11月5日 9时00分、2026/11/05
_DATETIME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?",
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:：时]\s*(\d{1,2})\s*分?(?:\s*(\d{1,2})\s*秒)?",
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?!\s*\d)",  # 仅日期，如 2026年11月03日
    )
)
_DATE_ONLY = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})\s*日?")

# 详情页字段标签
_PUBLISH_LABELS = ("发布时间", "发布日期", "公告时间", "公示时间", "发布日期时间")
_DEADLINE_LABELS = (
    "投标截止时间",
    "投标截止日期",
    "响应文件递交截止时间",
    "响应截止时间",
    "报价截止时间",
    "递交截止时间",
    "截止时间",
    "截止日期",
)
_CANCEL_LABELS = ("取消", "终止", "废标", "撤项")
_CANCEL_REASON_LABELS = ("取消原因", "终止原因", "废标原因")
_PROJECT_LABELS = ("项目编号", "项目编码", "采购编号", "招标编号")

# 合理公告年份窗口，过滤资质/业绩说明等场景下的异常或历史日期
_MIN_YEAR = 2000
_MAX_YEAR = 2100

# 标题里用于判定公告类型的关键词
_CORRECTION_KEYWORDS = ("更正", "澄清", "变更", "修改", "延期", "答疑", "补充公告")
_CANCELLATION_KEYWORDS = ("取消", "终止", "废标", "撤项")


class ParseError(ValueError):
    """页面结构无法解析出必要字段。"""


@dataclass
class DetailData:
    """详情页原始解析结果。"""

    title: str
    project_code: str | None
    published_at: datetime | None
    deadline: datetime | None
    cancelled: bool
    cancellation_reason: str | None
    kind_hint: str  # original / correction / cancellation，来自标题与正文线索
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def parse_datetime(text: str | None) -> datetime | None:
    """从一段文本中解析出第一个日期时间；只有日期时按当天 00:00 处理。"""
    if not text:
        return None
    for pattern in _DATETIME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 3:  # 仅日期
            year, month, day = (int(g) for g in groups)
            return datetime(year, month, day)
        year, month, day, hour, minute = (int(g) for g in groups[:5])
        second = int(groups[5]) if len(groups) >= 6 and groups[5] else 0
        return datetime(year, month, day, hour, minute, second)
    match = _DATE_ONLY.search(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return datetime(year, month, day)
    return None


def same_site(url: str, base_url: str) -> bool:
    """判断两个 URL 是否同站点（scheme + host 一致；端口不同视为不同站点）。"""
    a, b = urlparse(url), urlparse(base_url)
    if not a.scheme or not a.netloc:
        return False
    return (a.scheme.lower(), a.netloc.lower()) == (b.scheme.lower(), b.netloc.lower())


def _soup(html: str) -> BeautifulSoup:
    # 优先 lxml，未安装时回退内置 html.parser
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - 取决于运行环境
        return BeautifulSoup(html, "html.parser")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


# --------------------------------------------------------------------------- #
# 列表页
# --------------------------------------------------------------------------- #
def extract_detail_links(html: str, list_url: str) -> list[tuple[str, str, str]]:
    """提取列表页中指向同站点详情页的链接。

    返回 ``(绝对URL, 链接文本, 行上下文)`` 去重列表，保持页面出现顺序。
    行上下文取链接所在 ``tr``/``li``/容器行的完整文本，通常包含项目编号，
    用于在详情页抓取失败时仍能判定该链接是否属于目标项目。
    导航/分页/登录类链接通过路径关键词排除。
    """
    soup = _soup(html)
    seen: set[str] = set()
    links: list[tuple[str, str, str]] = []
    skip_path = re.compile(r"(login|logout|register|index|home|about|contact|search|#|javascript)", re.I)

    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(list_url, anchor["href"].strip())
        if not absolute.startswith(("http://", "https://")):
            continue
        if not same_site(absolute, list_url):
            continue  # 只抓同站点链接，外站公告不纳入
        parsed = urlparse(absolute)
        if skip_path.search(parsed.path) or parsed.path.rstrip("/") == urlparse(list_url).path.rstrip("/"):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        row = anchor.find_parent(["tr", "li", "dd", "article"]) or anchor.parent
        context = _clean(row.get_text(" ")) if row is not None else ""
        links.append((absolute, _clean(anchor.get_text()), context))
    return links


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
def _meta_value(soup: BeautifulSoup, key: str) -> str | None:
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        if name == key.lower():
            return meta.get("content")
    return None


def _data_field(soup: BeautifulSoup, name: str) -> str | None:
    """读取 ``data-field="name"`` 元素的取值。"""
    node = soup.find(attrs={"data-field": name})
    if node is None:
        return None
    value = node.get("data-value")
    if value is not None:
        return value.strip()
    return _clean(node.get_text())


def _labeled_value(soup: BeautifulSoup, labels: tuple[str, ...]) -> str | None:
    """按“标签：值”结构取值，兼容 <td>/<th>/<dt><dd>/<li>/<p> 等布局。"""
    label_set = set(labels)

    # 1) <th>标签</th><td>值</td> 或 <dt>/<dd>
    for label_node in soup.find_all(["th", "dt"]):
        if _clean(label_node.get_text()).rstrip("：:") in label_set:
            sibling = label_node.find_next(["td", "dd"])
            if sibling is not None:
                return _clean(sibling.get_text())

    # 2) 任意元素中“标签：值”同行文本
    pattern = re.compile(
        r"(?:^|\s)(" + "|".join(re.escape(x) for x in labels) + r")\s*[:：]\s*(.+?)(?:\s{2,}|$)"
    )
    for node in soup.find_all(["td", "li", "p", "span", "div"]):
        # 跳过还包含同层标签的容器，避免把整块正文当成值
        if node.find(["td", "li", "p", "div"]):
            continue
        text = _clean(node.get_text(" "))
        match = pattern.search(text)
        if match:
            return _clean(match.group(2))
    return None


def _extract_project_code(soup: BeautifulSoup) -> str | None:
    value = _data_field(soup, "project-code") or _labeled_value(soup, _PROJECT_LABELS)
    return _clean(value) or None


def _extract_published_at(soup: BeautifulSoup) -> datetime | None:
    raw = (
        _data_field(soup, "published-at")
        or _meta_value(soup, "article:published_time")
        or _labeled_value(soup, _PUBLISH_LABELS)
    )
    return parse_datetime(raw)


def _extract_deadline(soup: BeautifulSoup, body_text: str) -> datetime | None:
    # 1) 结构化标记
    raw = _data_field(soup, "deadline")
    if raw:
        return parse_datetime(raw)
    # 2) 标签匹配
    raw = _labeled_value(soup, _DEADLINE_LABELS)
    if raw:
        return parse_datetime(raw)
    # 3) 正文正则兜底：逐块级元素（p/li/td/div 等）扫描含时限语义的文本，
    #    避免 get_text 把发布时间与截止时间拼进同一段导致误取发布时间；
    #    块内再按句末标点/逗号切分，兼容“应在 X 之前完成递交，逾期……”的写法
    time_limit_hint = re.compile(r"截止|递交|提交|受理|送达|逾期|之前|前完成")
    candidate_blocks: list[str] = [body_text]
    for node in soup.find_all(["p", "li", "td", "div", "dd"]):
        if node.find(["p", "li", "td", "div"]):
            continue  # 只取叶子块，避免重复扫描容器
        candidate_blocks.append(_clean(node.get_text(" ")))

    # 资质/业绩语境（如“近三年（2023年1月1日起）完成 2 项业绩”）不是投标时限
    history_hint = re.compile(r"近\s*\d+\s*年|业绩|资质|经验|成立以来|至今|起?算")
    for block in candidate_blocks:
        if "发布时间" in block or not time_limit_hint.search(block):
            continue
        for sentence in re.split(r"[。；;，,\n]", block):
            if history_hint.search(sentence):
                continue
            dt = parse_datetime(sentence)
            # 年份窗口保护：过滤明显不属于公告时效的异常年份
            if dt and _MIN_YEAR <= dt.year <= _MAX_YEAR:
                return dt
    return None


def _extract_cancelled(soup: BeautifulSoup, body_text: str) -> tuple[bool, str | None]:
    flagged = soup.find(attrs={"data-field": "cancelled"})
    if flagged is not None:
        flag = (flagged.get("data-value") or flagged.get_text()).strip().lower()
        if flag in ("true", "1", "yes", "是"):
            reason = _data_field(soup, "cancellation-reason") or _labeled_value(soup, _CANCEL_REASON_LABELS)
            return True, _clean(reason) or None
        return False, None

    reason = _labeled_value(soup, _CANCEL_REASON_LABELS)
    if reason:
        return True, _clean(reason)
    # 显式取消句式，避免“不取消”之类的否定误判
    if re.search(r"(?:本项目|本次采购|本次招标|该项目)[^。；\n]{0,12}(?:取消|终止|废标)", body_text):
        return True, None
    if re.search(r"(?:取消|终止|废标)(?:本次|本项目|该项目)?(?:采购|招标|投标)", body_text):
        return True, None
    return False, None


def _detect_kind(title: str, cancelled: bool) -> str:
    if cancelled or any(k in title for k in _CANCELLATION_KEYWORDS):
        return "cancellation"
    if any(k in title for k in _CORRECTION_KEYWORDS):
        return "correction"
    return "original"


def parse_detail(html: str, url: str) -> DetailData:
    """解析公告详情页，提取合并所需的全部字段。"""
    soup = _soup(html)

    title_node = soup.find("title")
    h1 = soup.find("h1")
    title = _clean(h1.get_text() if h1 else (title_node.get_text() if title_node else ""))
    if not title:
        raise ParseError(f"详情页缺少标题：{url}")

    body_text = _clean(soup.get_text(" "))
    project_code = _extract_project_code(soup)
    published_at = _extract_published_at(soup)
    deadline = _extract_deadline(soup, body_text)
    cancelled, reason = _extract_cancelled(soup, body_text)
    kind_hint = _detect_kind(title, cancelled)

    warnings: list[str] = []
    if published_at is None:
        warnings.append("未能解析发布时间，无法参与按时间合并")

    return DetailData(
        title=title,
        project_code=project_code,
        published_at=published_at,
        deadline=deadline,
        cancelled=cancelled,
        cancellation_reason=reason,
        kind_hint=kind_hint,
        warnings=warnings,
    )
