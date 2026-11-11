"""端到端测试：对 sample-site 启动本地静态服务器，通过 FastAPI 跑完整采集链路。"""

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
    def log_message(self, *args):  # 静默测试输出
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


def _collect(client, list_url, code):
    resp = client.post(
        "/api/v1/collect",
        json={"list_url": list_url, "project_code": code, "timeout": 5},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_project_001_merge_corrections_and_deadline_extension(client, static_server):
    data = _collect(client, static_server, "GSB-2026-001")

    assert data["status"] == "open"
    assert data["can_bid"] is True
    assert data["effective_deadline"].startswith("2026-11-15T14:00:00")
    assert data["cancelled"] is False

    # 三条公告按发布时间排序：原公告 → 澄清(未改期) → 延期
    kinds = [n["kind"] for n in data["notices"]]
    assert kinds == ["original", "correction", "correction"]
    published = [n["published_at"] for n in data["notices"]]
    assert published == sorted(published)

    # 澄清公告未提截止时间 → 沿用原时间；延期公告覆盖
    assert data["notices"][1]["deadline"] is None
    assert data["notices"][2]["deadline"].startswith("2026-11-15T14:00:00")

    roles = {s["role"] for s in data["sources"]}
    assert {"list", "original", "correction"} <= roles


def test_project_002_cancelled_is_not_biddable(client, static_server):
    data = _collect(client, static_server, "GSB-2026-002")

    assert data["status"] == "closed"
    assert data["can_bid"] is False
    assert data["cancelled"] is True
    # 取消后不存在“有效截止时间”，旧时间只能在 notices 明细中追溯
    assert data["effective_deadline"] is None
    assert any("取消" in w for w in data["warnings"])
    assert [n["kind"] for n in data["notices"]] == ["original", "cancellation"]
    assert data["notices"][0]["deadline"].startswith("2026-11-12T14:30:00")


def test_project_003_failed_detail_forces_pending_without_deadline(client, static_server):
    data = _collect(client, static_server, "GSB-2026-003")

    assert data["status"] == "pending"
    assert data["can_bid"] is False
    # 关键约束：有关联页面抓取失败时，绝不能把原公告的旧截止时间返回为最终期限
    assert data["effective_deadline"] is None
    assert any("抓取失败" in w for w in data["warnings"])
    assert any(s["role"] == "failed" for s in data["sources"])


def test_unknown_project_returns_not_found(client, static_server):
    data = _collect(client, static_server, "GSB-2099-NOPE")
    assert data["status"] == "not_found"
    assert data["effective_deadline"] is None
    # 无关项目的坏链接不应被计入本项目的关联失败
    assert not any(s["role"] == "failed" for s in data["sources"])


def test_external_and_nav_links_ignored(client, static_server):
    data = _collect(client, static_server, "GSB-2026-001")
    all_urls = [s["url"] for s in data["sources"]] + [n["url"] for n in data["notices"]]
    assert not any("example.gov" in u for u in all_urls)
    assert not any("login" in u for u in all_urls)


def test_get_endpoint_works_the_same(client, static_server):
    resp = client.get(
        "/api/v1/collect",
        params={"list_url": static_server, "project_code": "GSB-2026-001"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


def test_bad_list_url_returns_502(client):
    resp = client.post(
        "/api/v1/collect",
        json={"list_url": "http://127.0.0.1:1/never-listens.html", "project_code": "X", "timeout": 2},
    )
    assert resp.status_code == 502


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
