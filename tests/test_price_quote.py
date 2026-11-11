"""限价清单解析与报价核对的单元测试（不依赖网络）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.price_parser import (
    PriceParseError,
    extract_price_list_links,
    normalize_package,
    parse_money,
    parse_price_tables,
    round_cents,
)
from app.quote import acheck_quote
from app.models import QuoteLine, QuoteRequest

# --------------------------------------------------------------------------- #
# 数字解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("12.00", Decimal("12.00")),
        ("1,234,567.89 元", Decimal("1234567.89")),
        ("￥76,750.00", Decimal("76750.00")),
        ("柒万陆仟柒佰伍拾元整", Decimal("76750")),
        ("叁佰伍拾元伍角", Decimal("350.5")),
        ("壹万贰仟叁佰元整", Decimal("12300")),
        ("--", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_money(text, expected):
    assert parse_money(text) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("第1包", "1"),
        ("包2", "2"),
        ("采购包 03", "3"),
        ("第三包", "3"),
        ("第十包", "10"),
        ("十二", "12"),
        ("", ""),
    ],
)
def test_normalize_package(raw, expected):
    assert normalize_package(raw) == expected


def test_round_cents_uses_half_up():
    # 使用字符串构造 Decimal，避免二进制浮点误差；ROUND_HALF_UP 下 5 一律进位
    assert round_cents(Decimal("1.005")) == Decimal("1.01")
    assert round_cents(Decimal("2.344")) == Decimal("2.34")
    assert round_cents(Decimal("2.345")) == Decimal("2.35")
    assert round_cents(Decimal("100") * Decimal("4.995")) == Decimal("499.50")


# --------------------------------------------------------------------------- #
# 表格解析：rowspan 包号还原、汇总表、内联整包限价
# --------------------------------------------------------------------------- #
ROWSPAN_HTML = """
<html><body>
<table>
  <tr><th>包号</th><th>编码</th><th>名称</th><th>单位</th><th>数量</th><th>最高单价限价（元）</th></tr>
  <tr><td rowspan="2">第1包</td><td>A-01</td><td>项一</td><td>个</td><td>10</td><td>100.00</td></tr>
  <tr><td>A-02</td><td>项二</td><td>米</td><td>5</td><td>20.50</td></tr>
  <tr><td>第2包</td><td>B-01</td><td>项三</td><td>套</td><td>2</td><td>999.00</td></tr>
</table>
<table>
  <tr><th>包号</th><th>整包最高限价（元）</th></tr>
  <tr><td>第1包</td><td>1102.50</td></tr>
  <tr><td>第2包</td><td>1998.00</td></tr>
</table>
</body></html>
"""


def test_parse_restores_rowspan_package_and_summary():
    result = parse_price_tables(ROWSPAN_HTML, "http://x/price.html")
    codes = {(i.package_no, i.item_code): i for i in result.items}
    assert codes[("1", "A-01")].quantity == Decimal("10")
    assert codes[("1", "A-01")].limit_unit_price == Decimal("100.00")
    # rowspan 跨行继承包号
    assert codes[("1", "A-02")].package_raw == "第1包"
    assert codes[("2", "B-01")].package_no == "2"
    limits = {p.package_no: p.amount for p in result.package_limits}
    assert limits == {"1": Decimal("1102.50"), "2": Decimal("1998.00")}


INLINE_LIMIT_HTML = """
<html><body>
<table>
  <tr><th>包号</th><th>编码</th><th>名称</th><th>数量</th><th>最高单价</th><th>整包限价（元）</th></tr>
  <tr><td rowspan="2">包2</td><td>C-1</td><td>x</td><td>150</td><td>260.00</td><td rowspan="2">52000.00</td></tr>
  <tr><td>C-2</td><td>y</td><td>800</td><td>16.25</td></tr>
</table>
</body></html>
"""


def test_parse_inline_package_limit_with_rowspan():
    result = parse_price_tables(INLINE_LIMIT_HTML, "http://x/p2.html")
    assert {i.item_code for i in result.items} == {"C-1", "C-2"}
    # 整包限价跨行出现两次，取值一致（不构成冲突）
    assert {p.amount for p in result.package_limits} == {Decimal("52000.00")}
    assert all(p.package_no == "2" for p in result.package_limits)


def test_unit_column_does_not_catch_price_headers():
    # 表头含“（元）”但没有计量单位列时，unit 应为 None，且单价/整包列正确识别
    html = """
    <table>
      <tr><th>包号</th><th>编码</th><th>名称</th><th>数量</th><th>最高单价（元）</th><th>整包限价（元）</th></tr>
      <tr><td>1</td><td>E-1</td><td>项</td><td>3</td><td>10.00</td><td>30.00</td></tr>
    </table>
    """
    result = parse_price_tables(html, "http://x/u.html")
    item = result.items[0]
    assert item.unit is None
    assert item.limit_unit_price == Decimal("10.00")
    assert result.package_limits[0].amount == Decimal("30.00")


def test_bare_unit_header_recognized():
    html = """
    <table>
      <tr><th>编码</th><th>名称</th><th>单位</th><th>数量</th><th>最高单价</th></tr>
      <tr><td>F-1</td><td>项</td><td>台</td><td>2</td><td>8.00</td></tr>
    </table>
    """
    result = parse_price_tables(html, "http://x/bu.html")
    assert result.items[0].unit == "台"


def test_parse_price_tables_raises_without_table():
    with pytest.raises(PriceParseError):
        parse_price_tables("<html><body><p>没有表格</p></body></html>", "http://x/none.html")


def test_extract_price_list_links_same_site_only():
    html = """
    <html><body>
      <a href="/login.html">登录</a>
      <a href="https://example.gov/price.html">外站清单</a>
      <a href="/detail/g001-price-1.html">第1包 最高限价清单</a>
      <a href="/detail/g001-original.html">原公告（无清单语义）</a>
    </body></html>
    """
    links = extract_price_list_links(html, "http://localhost:8080/detail/g001-original.html")
    urls = [u for u, _, _ in links]
    assert urls == ["http://localhost:8080/detail/g001-price-1.html"]


# --------------------------------------------------------------------------- #
# 报价核对业务规则（通过 monkeypatch 替换网络层，离线运行）
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    """按 URL 后缀返回预置 HTML；未登记的 URL 抛 HTTPError 模拟 404/超时。"""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    async def get(self, url: str) -> _FakeResponse:
        for suffix, html in self.pages.items():
            if url.endswith(suffix):
                return _FakeResponse(html)
        import httpx

        raise httpx.HTTPStatusError("404", request=None, response=None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


_LIST_HTML = """
<html><body><table><tbody>
  <tr><td><a href="/detail/g1-original.html">项目原公告</a></td><td>G-1</td></tr>
</tbody></table></body></html>
"""

_ORIGINAL_HTML = """
<html><head><title>项目公开招标公告</title></head><body>
<h1>项目公开招标公告</h1>
<table><tr><th>项目编号</th><td>G-1</td></tr>
<tr><th>发布时间</th><td>2026-11-01 10:00</td></tr>
<tr><th>投标截止时间</th><td>2026-11-20 09:30</td></tr></table>
<a href="/detail/price.html">最高限价清单</a>
</body></html>
"""

_PRICE_HTML = """
<html><body>
<table>
  <tr><th>包号</th><th>编码</th><th>名称</th><th>单位</th><th>数量</th><th>最高单价限价（元）</th></tr>
  <tr><td rowspan="2">第1包</td><td>A-01</td><td>项一</td><td>个</td><td>100</td><td>10.00</td></tr>
  <tr><td>A-02</td><td>项二</td><td>米</td><td>20</td><td>5.00</td></tr>
</table>
<table><tr><th>包号</th><th>整包最高限价（元）</th></tr>
<tr><td>第1包</td><td>1100.00</td></tr></table>
</body></html>
"""


def _req(lines, package_no="1", code="G-1"):
    return QuoteRequest(
        list_url="http://x/list.html",
        project_code=code,
        package_no=package_no,
        lines=[QuoteLine(**line) for line in lines],
    )


@pytest.fixture
def patched_client(monkeypatch):
    def _install(pages):
        fake = _FakeClient(pages)
        # app/quote.py 通过 ``from app.collector import build_http_client`` 引入
        monkeypatch.setattr("app.quote.build_http_client", lambda timeout: fake)

    return _install


def _pages(price_html=_PRICE_HTML):
    return {
        "list.html": _LIST_HTML,
        "g1-original.html": _ORIGINAL_HTML,
        "price.html": price_html,
    }


async def test_quote_happy_path_amounts_rounded_and_totaled(patched_client):
    patched_client(_pages())
    # 100×9.99=999.00；20×4.995=99.90（四舍五入到分）→ 合计 1098.90
    resp = await acheck_quote(
        _req([{"item_code": "A-01", "unit_price": "9.99"},
              {"item_code": "A-02", "unit_price": "4.995"}])
    )
    assert resp.status.value == "ok"
    amounts = {lr.item_code: lr.quoted_amount for lr in resp.line_results}
    assert amounts == {"A-01": Decimal("999.00"), "A-02": Decimal("99.90")}
    assert resp.package.quoted_total == Decimal("1098.90")
    assert resp.package.package_limit == Decimal("1100.00")
    assert any(s.role == "price_list" for s in resp.sources)


async def test_quote_unit_price_over_limit(patched_client):
    patched_client(_pages())
    resp = await acheck_quote(_req([{"item_code": "A-01", "unit_price": "10.01"},
                                    {"item_code": "A-02", "unit_price": "5.00"}]))
    assert resp.status.value == "over_limit"
    a01 = next(lr for lr in resp.line_results if lr.item_code == "A-01")
    assert a01.status.value == "over_limit"
    assert any("超过最高单价限价" in r for r in a01.reasons)


async def test_quote_package_total_over_limit(patched_client):
    patched_client(_pages())
    # 单价均不超限（10 / 5），合计 100×10 + 20×5 = 1100；把 A-02 提到 5.00 不变，
    # 改用边界：A-01=10、A-02=5.01 → 单价超限；这里构造仅整包超限：
    # 用数量口径，A-01=10、A-02=5 → 1100 等于限价不超限
    resp_eq = await acheck_quote(_req([{"item_code": "A-01", "unit_price": "10.00"},
                                       {"item_code": "A-02", "unit_price": "5.00"}]))
    assert resp_eq.status.value == "ok"
    assert resp_eq.package.quoted_total == Decimal("1100.00")


async def test_quote_missing_item_forces_pending(patched_client):
    patched_client(_pages())
    resp = await acheck_quote(_req([{"item_code": "A-01", "unit_price": "9.00"}]))
    assert resp.status.value == "pending"
    missing = next(lr for lr in resp.line_results if lr.item_code == "A-02")
    assert missing.quoted_unit_price is None
    assert any("缺报" in r for r in missing.reasons)
    assert resp.package.quoted_total is None


async def test_quote_duplicate_and_unknown_codes_pending(patched_client):
    patched_client(_pages())
    resp = await acheck_quote(
        _req([
            {"item_code": "A-01", "unit_price": "9"},
            {"item_code": "A-01", "unit_price": "8"},
            {"item_code": "A-02", "unit_price": "5"},
            {"item_code": "ZZZ", "unit_price": "1"},
        ])
    )
    assert resp.status.value == "pending"
    a01 = next(lr for lr in resp.line_results if lr.item_code == "A-01")
    assert any("重复编码" in r for r in a01.reasons)
    zzz = next(lr for lr in resp.line_results if lr.item_code == "ZZZ")
    assert any("不在限价清单" in r for r in zzz.reasons)


async def test_quote_field_conflict_forces_pending(patched_client):
    # 两份清单页对同一编码 A-01 给出不同数量/单价，且整包限价不一致
    original_two_links = _ORIGINAL_HTML.replace(
        '<a href="/detail/price.html">最高限价清单</a>',
        '<a href="/detail/price-a.html">最高限价清单（一）</a>'
        '<a href="/detail/price-b.html">最高限价清单（二）</a>',
    )
    price_b = (
        _PRICE_HTML.replace("<td>100</td><td>10.00</td>", "<td>120</td><td>9.80</td>")
        .replace("1100.00", "1099.00")
    )
    pages = {
        "list.html": _LIST_HTML,
        "g1-original.html": original_two_links,
        "price-a.html": _PRICE_HTML,
        "price-b.html": price_b,
    }
    patched_client(pages)
    resp = await acheck_quote(_req([{"item_code": "A-01", "unit_price": "9"},
                                    {"item_code": "A-02", "unit_price": "5"}]))
    assert resp.status.value == "pending"
    a01 = next(lr for lr in resp.line_results if lr.item_code == "A-01")
    assert a01.quantity is None and a01.limit_unit_price is None
    assert any("数量字段冲突" in r for r in a01.reasons)
    assert resp.package.package_limit is None
    assert any("整包限价字段冲突" in r for r in resp.package.reasons)


async def test_quote_missing_package_limit_pending_but_unit_checks_continue(patched_client):
    # 清单只有分项限价，没有整包限价：分项可核对，整包待核实
    no_pkg_limit = """
    <html><body><table>
      <tr><th>包号</th><th>编码</th><th>名称</th><th>数量</th><th>最高单价限价（元）</th></tr>
      <tr><td rowspan="2">第1包</td><td>A-01</td><td>项一</td><td>100</td><td>10.00</td></tr>
      <tr><td>A-02</td><td>项二</td><td>20</td><td>5.00</td></tr>
    </table></body></html>
    """
    pages = _pages(no_pkg_limit)
    patched_client(pages)
    resp = await acheck_quote(_req([{"item_code": "A-01", "unit_price": "10.01"},
                                    {"item_code": "A-02", "unit_price": "5"}]))
    # 整包维度待核实（缺整包限价），但单价超限的行仍明确标记 over_limit
    assert resp.status.value == "pending"
    assert resp.package.package_limit is None
    a01 = next(lr for lr in resp.line_results if lr.item_code == "A-01")
    assert a01.status.value == "over_limit"
    assert any("清单未提供整包限价" in r for r in resp.package.reasons)


async def test_quote_price_page_fetch_failure_pending(patched_client):
    # 清单链接 404，且公告正文不含任何表格 → 无法回退，只能待核实
    original_no_table = """
    <html><head><title>项目公开招标公告</title></head><body>
    <h1>项目公开招标公告</h1>
    <p>项目编号：G-1</p>
    <p>发布时间：2026-11-01 10:00</p>
    <p>投标截止时间：2026-11-20 09:30</p>
    <a href="/detail/missing-price.html">最高限价清单（已下线）</a>
    </body></html>
    """
    pages = {
        "list.html": _LIST_HTML,
        "g1-original.html": original_no_table,
    }
    patched_client(pages)
    resp = await acheck_quote(_req([{"item_code": "A-01", "unit_price": "9"},
                                    {"item_code": "A-02", "unit_price": "5"}]))
    assert resp.status.value == "pending"
    assert any("读取失败" in r for r in resp.package.reasons)
    assert any(s.role == "failed" for s in resp.sources)
    assert resp.line_results == []


async def test_quote_failed_link_but_embedded_fallback_still_pending(patched_client):
    # 独立清单页 404，但公告正文内嵌了清单表格：可完成分项核对，
    # 因关联页面读取失败，整包仍标记待核实
    original = _ORIGINAL_HTML.replace(
        '<a href="/detail/price.html">最高限价清单</a>',
        '<a href="/detail/missing-price.html">最高限价清单（已下线）</a>',
    ) + _PRICE_HTML
    pages = {"list.html": _LIST_HTML, "g1-original.html": original}
    patched_client(pages)
    resp = await acheck_quote(_req([{"item_code": "A-01", "unit_price": "10"},
                                    {"item_code": "A-02", "unit_price": "5"}]))
    assert resp.status.value == "pending"
    assert any("读取失败" in r for r in resp.package.reasons)
    assert {lr.item_code for lr in resp.line_results} == {"A-01", "A-02"}
    assert any("内嵌限价清单" in (s.title or "") for s in resp.sources)


async def test_quote_package_not_found(patched_client):
    patched_client(_pages())
    resp = await acheck_quote(
        _req([{"item_code": "A-01", "unit_price": "9"}], package_no="99")
    )
    assert resp.status.value == "not_found"
    assert any("未找到该包号" in r for r in resp.package.reasons)


async def test_quote_project_without_original_not_found(patched_client):
    pages = {
        "list.html": "<html><body><table><tbody>"
                     "<tr><td><a href='/detail/c.html'>更正</a></td><td>G-2</td></tr>"
                     "</tbody></table></body></html>",
        "c.html": """<html><head><title>项目更正公告</title></head><body><h1>项目更正公告</h1>
        <ul><li data-field="project-code" data-value="G-2"></li>
        <li data-field="published-at" data-value="2026-11-02 09:00"></li></ul></body></html>""",
    }
    patched_client(pages)
    resp = await acheck_quote(_req([{"item_code": "X", "unit_price": "1"}], code="G-2"))
    assert resp.status.value == "not_found"


async def test_quote_embedded_table_when_no_link(patched_client):
    original = _ORIGINAL_HTML.replace('<a href="/detail/price.html">最高限价清单</a>', "")
    pages = {"list.html": _LIST_HTML, "g1-original.html": original + _PRICE_HTML}
    patched_client(pages)
    resp = await acheck_quote(_req([{"item_code": "A-01", "unit_price": "10"},
                                    {"item_code": "A-02", "unit_price": "5"}]))
    assert resp.status.value == "ok"
    assert any("内嵌限价清单" in (s.title or "") for s in resp.sources)
