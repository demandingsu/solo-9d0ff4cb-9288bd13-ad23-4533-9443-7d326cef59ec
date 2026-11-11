"""限价清单 HTML 解析。

职责：
1. 从公告详情页发现指向同站“限价清单/采购需求/报价明细”页面的链接；
2. 解析清单表格，还原 ``rowspan``/``colspan`` 合并单元格（包号常跨行只写一次），
   输出每个清单项的：包号、编码、名称、数量、单位、最高单价；
3. 解析整包限价：支持独立“包预算汇总表”，也支持明细表内的整包限价行。

解析器只做结构提取与数字标准化；跨表/跨页面的字段冲突判定在 ``app/quote.py`` 中完成。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.parser import _clean, _soup, same_site

# --------------------------------------------------------------------------- #
# 表头关键词（按列匹配，命中即认定该列的语义）
# --------------------------------------------------------------------------- #
_HEADER_PACKAGE = ("包号", "采购包", "分包号", "标包号", "标段号", "包别", "标包", "分包", "包段")
_HEADER_CODE = ("编码", "代码", "品目号", "清单号", "物料编号", "货物编号", "设备编号")
_HEADER_NAME = ("名称", "品名", "项目名称", "货物名称", "服务名称", "清单项名称", "品目名称", "设备名称")
_HEADER_QTY = ("数量", "工程量", "预估量", "预计数量", "采购数量")
# “计量单位”优先；裸“单位”放最后并要求表头恰好等于“单位”，避免“整包限价（元）/单价（元）”误匹配
_HEADER_UNIT = ("计量单位",)
# 注意：先匹配“单价限价”类，再由整包关键词兜底
_HEADER_UNIT_PRICE = (
    "最高单价限价",
    "最高单价",
    "单价限价",
    "限价单价",
    "预算单价",
    "单价最高限价",
    "最高限价单价",
    "单价（元）",
    "单价(元)",
    "综合单价",
    "单价限价（元）",
)
_HEADER_PACKAGE_LIMIT = (
    "整包限价",
    "包限价",
    "采购包限价",
    "分包限价",
    "包最高限价",
    "整包最高限价",
    "采购包预算",
    "分包预算",
    "包预算金额",
    "包预算",
    "预算金额",
    "最高限价",
    "采购预算",
    "预算总价",
)

# 链接发现：href/文本/行上下文中出现这些词，才可能是限价清单页
_PRICE_LINK_KEYWORDS = re.compile(r"限价|清单|预算|报价|采购需求|工程量|明细|标的")
_NAV_SKIP = re.compile(r"login|logout|register|index|home|about|contact|search|#|javascript", re.I)

# 中文大写数字（金额）支持
_CN_DIGITS = {
    "零": 0, "〇": 0, "壹": 1, "一": 1, "贰": 2, "二": 2, "两": 2, "叁": 3, "三": 3,
    "肆": 4, "四": 4, "伍": 5, "五": 5, "陆": 6, "六": 6, "柒": 7, "七": 7,
    "捌": 8, "八": 8, "玖": 9, "九": 9,
}
_CN_SMALL_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG_UNITS = {"万": 10_000, "萬": 10_000, "亿": 100_000_000, "億": 100_000_000}


class PriceParseError(ValueError):
    """限价清单结构无法解析。"""


@dataclass
class PriceItem:
    """限价清单中的一个明细项（已还原跨行包号）。"""

    package_no: str           # 规范化后的包号
    package_raw: str          # 页面原始包号文本
    item_code: str
    item_name: str | None
    quantity: Decimal | None
    unit: str | None
    limit_unit_price: Decimal | None
    source_url: str
    table_index: int


@dataclass
class PackageLimit:
    """整包限价（汇总表或明细表内整包行）。"""

    package_no: str
    package_raw: str
    amount: Decimal
    source_url: str
    table_index: int


@dataclass
class PriceTableParseResult:
    """一个页面的全部解析结果。"""

    items: list[PriceItem] = field(default_factory=list)
    package_limits: list[PackageLimit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 数字 / 包号工具
# --------------------------------------------------------------------------- #
def parse_money(text: str | None) -> Decimal | None:
    """解析金额/单价文本，支持阿拉伯数字（含千分位、元后缀）与中文大写金额。

    无法解析时返回 None。
    """
    if not text:
        return None
    raw = _clean(text)
    if not raw:
        return None

    # 1) 阿拉伯数字：取第一个数值（允许千分位、小数、人民币符号、元后缀）
    match = re.search(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?", raw)
    if match:
        try:
            return Decimal(match.group(0).replace(",", ""))
        except InvalidOperation:
            return None

    # 2) 中文大写金额，如 “壹万贰仟叁佰元整”“叁佰伍拾元伍角”
    if any(ch in _CN_DIGITS or ch in _CN_SMALL_UNITS or ch in _CN_BIG_UNITS for ch in raw):
        return _parse_chinese_money(raw)
    return None


def _parse_chinese_money(text: str) -> Decimal | None:
    total = 0
    section = 0          # 当前“万/亿”小节累计
    number = 0           # 当前数字（等待小单位）
    saw_any = False
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
            saw_any = True
        elif ch in _CN_SMALL_UNITS:
            section += (number or 1) * _CN_SMALL_UNITS[ch]
            number = 0
            saw_any = True
        elif ch in _CN_BIG_UNITS:
            section += number
            total += section * _CN_BIG_UNITS[ch]
            section = 0
            number = 0
            saw_any = True
        elif ch in ("元", "圆", "整", "正"):
            break
    result = Decimal(total + section + number)
    # 角/分简单处理
    jiao = re.search(r"([零壹贰叁肆伍陆柒捌玖一二三四五六七八九])角", text)
    fen = re.search(r"([零壹贰叁肆伍陆柒捌玖一二三四五六七八九])分", text)
    if jiao:
        result += Decimal(_CN_DIGITS[jiao.group(1)]) / Decimal(10)
    if fen:
        result += Decimal(_CN_DIGITS[fen.group(1)]) / Decimal(100)
    return result if saw_any else None


def normalize_package(value: str | None) -> str:
    """规范化包号：提取数字；中文数字（一~十）转阿拉伯数字；无数字时保留去空白原文。"""
    if not value:
        return ""
    text = _clean(value)
    digits = re.findall(r"\d+", text)
    if digits:
        return str(int("".join(digits)))
    cn = "".join(ch for ch in text if ch in "一二三四五六七八九十")
    if cn:
        mapping = {ch: i for i, ch in enumerate("一二三四五六七八九", start=1)}
        if cn == "十":
            return "10"
        if cn.startswith("十"):
            return str(10 + mapping.get(cn[1:], 0))
        if "十" in cn:
            head, tail = cn.split("十", 1)
            return str(mapping.get(head, 1) * 10 + (mapping.get(tail, 0) if tail else 0))
        if len(cn) == 1:
            return str(mapping[cn])
    return text


def round_cents(amount: Decimal) -> Decimal:
    """四舍五入到分（银行家舍入不符合招标惯例，使用 ROUND_HALF_UP）。"""
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# 链接发现
# --------------------------------------------------------------------------- #
def extract_price_list_links(html: str, base_url: str) -> list[tuple[str, str, str]]:
    """发现详情页中指向同站限价清单页面的链接。

    返回 ``(绝对URL, 链接文本, 行上下文)`` 去重列表，保持页面顺序。
    判定依据：href 路径或锚文本/行上下文含“限价/清单/预算/采购需求/工程量/明细”等关键词，
    且与公告页同站点；导航/登录类链接排除。
    """
    soup = _soup(html)
    seen: set[str] = set()
    links: list[tuple[str, str, str]] = []

    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(base_url, anchor["href"].strip())
        if not absolute.startswith(("http://", "https://")):
            continue
        if not same_site(absolute, base_url):
            continue
        parsed = urlparse(absolute)
        if _NAV_SKIP.search(parsed.path) or parsed.path.rstrip("/") == urlparse(base_url).path.rstrip("/"):
            continue
        row = anchor.find_parent(["tr", "li", "dd", "article", "p", "div"]) or anchor.parent
        context = _clean(row.get_text(" ")) if row is not None else ""
        anchor_text = _clean(anchor.get_text())
        haystack = f"{parsed.path} {anchor_text} {context}"
        if not _PRICE_LINK_KEYWORDS.search(haystack):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append((absolute, anchor_text, context))
    return links


# --------------------------------------------------------------------------- #
# 表格解析（含合并单元格还原）
# --------------------------------------------------------------------------- #
def _expand_table(table) -> list[list[str]]:
    """把 table 展开为规则二维文本网格，rowspan/colspan 占位格用首格文本填充。"""
    pending: dict[int, tuple[str, int]] = {}  # col -> (跨行文本, 剩余行数)
    grid: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        row: list[str] = []
        col = 0
        ci = 0
        while ci < len(cells) or col in pending:
            if col in pending:
                text, remaining = pending[col]
                row.append(text)
                if remaining <= 1:
                    del pending[col]
                else:
                    pending[col] = (text, remaining - 1)
                col += 1
                continue
            if ci >= len(cells):
                break
            cell = cells[ci]
            ci += 1
            text = _clean(cell.get_text(" "))
            rowspan = int(cell.get("rowspan", 1) or 1)
            colspan = int(cell.get("colspan", 1) or 1)
            for offset in range(colspan):
                row.append(text)
                if rowspan > 1:
                    pending[col] = (text, rowspan - 1)
                col += 1
        if row:
            grid.append(row)
    return grid


def _match_column(headers: list[str], keywords: tuple[str, ...]) -> int | None:
    for idx, header in enumerate(headers):
        if any(k in header for k in keywords):
            return idx
    return None


def _match_unit_column(headers: list[str]) -> int | None:
    # 先匹配“计量单位”；再接受表头恰好为“单位”的列，防止子串误匹配
    idx = _match_column(headers, _HEADER_UNIT)
    if idx is not None:
        return idx
    for idx, header in enumerate(headers):
        if header.strip() == "单位":
            return idx
    return None


def _column_map(header_row: list[str]) -> dict[str, int]:
    """根据表头行文本映射字段 → 列下标。整包限价关键词让位于单价关键词。"""
    mapping: dict[str, int] = {}
    for field_name, keywords in (
        ("package", _HEADER_PACKAGE),
        ("code", _HEADER_CODE),
        ("name", _HEADER_NAME),
        ("qty", _HEADER_QTY),
        ("unit_price", _HEADER_UNIT_PRICE),
    ):
        idx = _match_column(header_row, keywords)
        if idx is not None:
            mapping[field_name] = idx
    unit_idx = _match_unit_column(header_row)
    if unit_idx is not None:
        mapping["unit"] = unit_idx
    # 整包限价列：不能与单价列同列
    pkg_idx = _match_column(header_row, _HEADER_PACKAGE_LIMIT)
    if pkg_idx is not None and pkg_idx != mapping.get("unit_price"):
        mapping["package_limit"] = pkg_idx
    return mapping


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def parse_price_tables(html: str, source_url: str) -> PriceTableParseResult:
    """解析一个页面中的全部限价清单表格。

    明细表判定：含“编码”列且含“数量/单价”列；
    汇总表判定：含“包号”列与“整包限价/预算”列但无“编码”列。
    包号列跨行（rowspan）时，展开网格后每行都能取到同一个包号。
    """
    soup = _soup(html)
    result = PriceTableParseResult()
    tables = soup.find_all("table")
    if not tables:
        raise PriceParseError(f"页面未包含任何表格：{source_url}")

    parsed_any = False
    for table_index, table in enumerate(tables):
        grid = _expand_table(table)
        if len(grid) < 2:
            continue

        # 选取关键词命中最多的一行作为表头
        best_idx, best_map, best_hits = None, {}, -1
        for ridx, header_row in enumerate(grid):
            mapping = _column_map(header_row)
            hits = len(mapping)
            if hits > best_hits:
                best_hits, best_idx, best_map = hits, ridx, mapping
        if best_idx is None or not best_map:
            continue

        is_detail = "code" in best_map and ("qty" in best_map or "unit_price" in best_map)
        is_summary = (
            "package" in best_map
            and "package_limit" in best_map
            and "code" not in best_map
        )
        if not (is_detail or is_summary):
            continue
        parsed_any = True

        current_package_raw = ""
        for row in grid[best_idx + 1 :]:
            if not any(_clean(c) for c in row):
                continue  # 全空行

            pkg_raw = _cell(row, best_map.get("package"))
            if pkg_raw:
                current_package_raw = pkg_raw
            pkg_no = normalize_package(current_package_raw)

            if is_summary:
                amount = parse_money(_cell(row, best_map.get("package_limit")))
                if pkg_no and amount is not None:
                    result.package_limits.append(
                        PackageLimit(
                            package_no=pkg_no,
                            package_raw=current_package_raw,
                            amount=amount,
                            source_url=source_url,
                            table_index=table_index,
                        )
                    )
                continue

            # 明细表
            code = _clean(_cell(row, best_map.get("code")))
            pkg_limit_inline = parse_money(_cell(row, best_map.get("package_limit")))
            # 明细表中的整包限价列（常 rowspan 跨行）：首行即可收集，重复值后续去重/冲突判定
            if pkg_no and pkg_limit_inline is not None:
                result.package_limits.append(
                    PackageLimit(
                        package_no=pkg_no,
                        package_raw=current_package_raw,
                        amount=pkg_limit_inline,
                        source_url=source_url,
                        table_index=table_index,
                    )
                )
            # 无编码且只有整包限价的“整包汇总行”
            if not code:
                continue

            quantity = parse_money(_cell(row, best_map.get("qty")))
            unit_price = parse_money(_cell(row, best_map.get("unit_price")))
            unit_text = _clean(_cell(row, best_map.get("unit")))
            name_text = _clean(_cell(row, best_map.get("name"))) or None

            if quantity is None and unit_price is None:
                result.warnings.append(
                    f"清单项 {code} 的数量与最高单价均无法解析（来源 {source_url}），需人工核实"
                )
            result.items.append(
                PriceItem(
                    package_no=pkg_no,
                    package_raw=current_package_raw,
                    item_code=code,
                    item_name=name_text,
                    quantity=quantity,
                    unit=unit_text or None,
                    limit_unit_price=unit_price,
                    source_url=source_url,
                    table_index=table_index,
                )
            )

    if not parsed_any:
        raise PriceParseError(f"未在页面表格中识别出限价清单结构：{source_url}")
    return result
