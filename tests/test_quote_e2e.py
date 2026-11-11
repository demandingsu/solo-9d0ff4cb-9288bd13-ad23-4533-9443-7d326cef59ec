"""报价核对端到端测试：本地静态服务器 + FastAPI TestClient，全链路离线可跑。"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample-site"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def static_server():
    handler = functools.partial(_QuietHandler, directory=str(SAMPLE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}/list.html"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _check(client, list_url, code, package_no, lines, timeout=5):
    resp = client.post(
        "/api/v1/quote-check",
        json={
            "list_url": list_url,
            "project_code": code,
            "package_no": package_no,
            "lines": lines,
            "timeout": timeout,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_quote_check_package1_rowspan_restore_and_conflict_detection(client, static_server):
    # 第1包：包号 rowspan 跨行还原；样例站同时挂了“字段冲突演练页”，
    # 两份清单对 YL-001 的数量/单价及整包限价不一致 → 整包待核实
    data = _check(
        client, static_server, "GSB-2026-001", "1",
        [
            {"item_code": "YL-001", "unit_price": "11.50"},
            {"item_code": "YL-002", "unit_price": "290.00"},
            {"item_code": "YL-003", "unit_price": "9.00"},
        ],
    )
    assert data["status"] == "pending"
    assert any("字段冲突" in r for r in data["package"]["reasons"])
    # rowspan 包号还原后，三个清单项都能被识别
    assert {lr["item_code"] for lr in data["line_results"]} == {
        "YL-001", "YL-002", "YL-003"
    }
    conflict_item = next(lr for lr in data["line_results"] if lr["item_code"] == "YL-001")
    assert conflict_item["quantity"] is None  # 1000 与 1200 冲突
    assert any(s["role"] == "price_list" for s in data["sources"])


def test_quote_check_package2_ok_and_inline_limit(client, static_server):
    # 第2包：整包限价写在明细表内并 rowspan 跨行
    data = _check(
        client, static_server, "GSB-2026-001", "2",
        [
            {"item_code": "YM-101", "unit_price": "260.00"},
            {"item_code": "YM-102", "unit_price": "16.25"},
        ],
    )
    assert data["status"] == "ok"
    assert data["package"]["package_limit"] == "52000.00"
    # 150×260 + 800×16.25 = 39000 + 13000 = 52000，等于限价不算超限
    assert data["package"]["quoted_total"] == "52000.00"
    amounts = {lr["item_code"]: lr["quoted_amount"] for lr in data["line_results"]}
    assert amounts == {"YM-101": "39000.00", "YM-102": "13000.00"}
    assert all(lr["status"] == "ok" for lr in data["line_results"])


def test_quote_check_unit_and_total_over_limit(client, static_server):
    data = _check(
        client, static_server, "GSB-2026-001", "2",
        [
            {"item_code": "YM-101", "unit_price": "270.00"},   # > 260
            {"item_code": "YM-102", "unit_price": "17.00"},    # > 16.25
        ],
    )
    assert data["status"] == "over_limit"
    # 40500 + 13600 = 54100 > 52000
    assert data["package"]["quoted_total"] == "54100.00"
    assert any("整包报价超过整包限价" in r for r in data["package"]["reasons"])
    assert all(lr["status"] == "over_limit" for lr in data["line_results"])
    assert all(
        any("超过最高单价限价" in r for r in lr["reasons"])
        for lr in data["line_results"]
    )


def test_quote_check_missing_item_pending(client, static_server):
    data = _check(
        client, static_server, "GSB-2026-001", "2",
        [{"item_code": "YM-101", "unit_price": "260.00"}],  # 缺报 YM-102
    )
    assert data["status"] == "pending"
    missing = next(lr for lr in data["line_results"] if lr["item_code"] == "YM-102")
    assert missing["quoted_unit_price"] is None
    assert any("缺报" in r for r in missing["reasons"])
    assert data["package"]["quoted_total"] is None
    assert any("缺报" in r for r in data["package"]["reasons"])


def test_quote_check_duplicate_and_unknown(client, static_server):
    data = _check(
        client, static_server, "GSB-2026-001", "2",
        [
            {"item_code": "YM-101", "unit_price": "260.00"},
            {"item_code": "YM-101", "unit_price": "250.00"},
            {"item_code": "NOPE-1", "unit_price": "1.00"},
        ],
    )
    assert data["status"] == "pending"
    dup = next(lr for lr in data["line_results"] if lr["item_code"] == "YM-101")
    assert any("重复编码" in r for r in dup["reasons"])
    unknown = next(lr for lr in data["line_results"] if lr["item_code"] == "NOPE-1")
    assert any("不在限价清单" in r for r in unknown["reasons"])


def test_quote_check_unknown_package_not_found(client, static_server):
    data = _check(
        client, static_server, "GSB-2026-001", "9",
        [{"item_code": "X", "unit_price": "1"}],
    )
    assert data["status"] == "not_found"
    assert any("未找到该包号" in r for r in data["package"]["reasons"])


def test_quote_check_unknown_project_not_found(client, static_server):
    data = _check(
        client, static_server, "GSB-2026-099", "1",
        [{"item_code": "X", "unit_price": "1"}],
    )
    # GSB-2026-099 有原公告但未链接限价清单
    assert data["status"] == "not_found"


def test_quote_check_failed_price_link_pending(client, static_server):
    # GSB-2026-003：独立清单页 404，但公告链了内嵌备份清单
    data = _check(
        client, static_server, "GSB-2026-003", "1",
        [
            {"item_code": "JC-001", "unit_price": "4000.00"},
            {"item_code": "JC-002", "unit_price": "80000.00"},
        ],
    )
    assert data["status"] == "pending"
    assert any("读取失败" in r for r in data["package"]["reasons"])
    failed = [s for s in data["sources"] if s["role"] == "failed"]
    assert any("g003-price-missing.html" in s["url"] for s in failed)
    # 内嵌备份清单是原公告直接链接的同站页面，被正常抓取解析，分项结果可输出供人工对照
    codes = {lr["item_code"] for lr in data["line_results"]}
    assert codes == {"JC-001", "JC-002"}
    price_sources = [s for s in data["sources"] if s["role"] == "price_list"]
    assert any("g003-price-embedded.html" in s["url"] for s in price_sources)


def test_quote_check_returns_source_links(client, static_server):
    data = _check(
        client, static_server, "GSB-2026-001", "2",
        [
            {"item_code": "YM-101", "unit_price": "260.00"},
            {"item_code": "YM-102", "unit_price": "16.25"},
        ],
    )
    roles = {s["role"] for s in data["sources"]}
    assert {"list", "original", "price_list"} <= roles
    # 每个清单项都带可追溯的来源 URL
    assert all(lr["source_url"] for lr in data["line_results"])


def test_quote_check_bad_list_url_returns_502(client):
    resp = client.post(
        "/api/v1/quote-check",
        json={
            "list_url": "http://127.0.0.1:1/never-listens.html",
            "project_code": "X",
            "package_no": "1",
            "lines": [{"item_code": "A", "unit_price": "1"}],
            "timeout": 2,
        },
    )
    assert resp.status_code == 502


def test_quote_check_rejects_empty_lines(client, static_server):
    resp = client.post(
        "/api/v1/quote-check",
        json={
            "list_url": static_server,
            "project_code": "GSB-2026-001",
            "package_no": "1",
            "lines": [],
        },
    )
    assert resp.status_code == 422
