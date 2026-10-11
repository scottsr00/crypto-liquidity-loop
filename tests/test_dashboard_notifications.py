import re

import httpx
from conftest import enter, quote
from fastapi.testclient import TestClient

from crypto_liquidity_loop.dashboard import create_app
from crypto_liquidity_loop.notifications import EmailDelivery


def test_dashboard_read_only_without_token(paper, monkeypatch):
    store, config, _ = paper
    monkeypatch.delenv("LOOP_DASHBOARD_TOKEN", raising=False)
    client = TestClient(create_app(store, config))
    response = client.get("/")
    assert response.status_code == 200
    assert "Controls are disabled" in response.text
    assert client.post("/controls/pause", data={"csrf": "fake"}).status_code == 403


def test_owner_auth_csrf_confirmation_and_persisted_controls(paper, monkeypatch):
    store, config, _ = paper
    token = "x" * 40
    monkeypatch.setenv("LOOP_DASHBOARD_TOKEN", token)
    client = TestClient(create_app(store, config))
    assert client.get("/api/state").status_code == 401
    assert client.post("/login", data={"password": "bad"}).status_code == 401
    response = client.post("/login", data={"password": token})
    assert response.status_code == 200
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', response.text).group(1)
    assert client.post("/controls/pause", data={"csrf": "wrong"}).status_code == 403
    assert client.post("/controls/liquidate", data={"csrf": csrf}).status_code == 400
    assert client.post("/controls/pause", data={"csrf": csrf}).status_code == 200
    assert store.rows("controls")[0]["status"] == "pending"
    assert not store.state()["paused"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_email_failure_does_not_block_halt_and_has_backoff(paper, monkeypatch):
    store, _, engine = paper
    for key in ("LOOP_RESEND_API_KEY", "LOOP_EMAIL_FROM", "LOOP_EMAIL_TO"):
        monkeypatch.setenv(key, "test-placeholder")

    def fail(*args, **kwargs):
        raise httpx.ConnectError("provider unavailable")

    monkeypatch.setattr(httpx, "post", fail)
    enter(engine)
    engine.tick(quote(86405, "98", "99", "1000"), 86405)
    EmailDelivery(store).deliver_once(86406)
    assert store.rows("alerts")[0]["status"] == "retry"
    assert store.rows("alerts")[0]["next_attempt"] > 86406
    engine.tick(quote(86410, "20", "21"), 86410)
    assert store.state()["halted"]


def test_email_unconfigured_and_deduplication(paper, monkeypatch):
    store, _, engine = paper
    monkeypatch.delenv("LOOP_RESEND_API_KEY", raising=False)
    engine.tick(None, 86400)
    engine.tick(None, 86405)
    assert len(store.rows("alerts")) == 1
    assert EmailDelivery(store).deliver_once(86406) == "unconfigured"
