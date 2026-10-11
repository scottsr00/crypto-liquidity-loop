import json

import pytest
from conftest import enter, quote

from crypto_liquidity_loop.domain import D, Product
from crypto_liquidity_loop.execution import PaperEngine
from crypto_liquidity_loop.storage import Store, encode
from crypto_liquidity_loop.worker import WorkerLock


def orders(store):
    return [json.loads(r["body"]) for r in store.rows("orders")]


def test_no_premature_or_touch_fill_and_no_duplicate_decision(paper):
    store, _, engine = paper
    enter(engine)
    assert not store.rows("fills")
    engine.tick(quote(86405, "99", "100"), 86405)
    assert not store.rows("fills")
    enter(engine)
    assert len(orders(store)) == 1


def test_partial_entry_retained_and_not_topped_up(paper):
    store, _, engine = paper
    enter(engine)
    engine.tick(quote(86405, "98", "99", "1"), 86405)
    assert D(store.state()["btc"]) == D(".01")
    engine.tick(quote(90000), 90000)
    assert orders(store)[0]["status"] == "canceled"
    engine.tick(quote(172800), 172800, {"action": "enter", "timestamp": 172800})
    assert len(orders(store)) == 1
    assert D(store.state()["btc"]) == D(".01")


def test_same_quote_cannot_repeat_partial_fill(paper):
    store, _, engine = paper
    enter(engine)
    q = quote(86405, "98", "99", "1")
    engine.tick(q, 86405)
    engine.tick(q, 86406)
    assert len(store.rows("fills")) == 1


def test_quote_before_order_creation_cannot_fill(paper):
    store, _, engine = paper
    enter(engine)
    engine.tick(quote(86399, "98", "99"), 86405)
    assert not store.rows("fills")


def test_pause_cancels_buy_retains_fills_and_exits_continue(paper):
    store, _, engine = paper
    enter(engine)
    engine.tick(quote(86405, "98", "99", "1"), 86405)
    store.request_control("pause")
    engine.tick(quote(86410, "98", "99"), 86410)
    assert store.state()["paused"]
    assert orders(store)[0]["status"] == "canceled"
    assert D(store.state()["btc"]) == D(".01")
    engine.tick(quote(172800), 172800, {"action": "exit", "timestamp": 172800})
    assert orders(store)[0]["side"] == "SELL"


def test_entry_expiration_is_anchored_to_close(paper):
    store, _, engine = paper
    engine.tick(quote(90000), 90000, {"action": "enter", "timestamp": 86400})
    assert not orders(store)
    assert any(r["kind"] == "missed_entry" for r in store.rows("events"))


def test_stale_data_expires_order_without_filling(paper):
    store, _, engine = paper
    enter(engine)
    engine.tick(quote(86405, "98", "99"), 90001)
    assert not store.rows("fills")
    assert orders(store)[0]["status"] == "canceled"
    assert store.state()["health"] == "stale_data"


def test_restart_preserves_partial_fill_and_reconciles(paper):
    store, config, engine = paper
    enter(engine)
    engine.tick(quote(86405, "98", "99", "1"), 86405)
    restored = Store(config.database)
    restored.initialize(config)
    restarted = PaperEngine(restored, config, Product())
    assert restarted.reconcile(86410)
    restarted.tick(quote(90000), 90000)
    assert len(restored.rows("fills")) == 1
    assert D(restored.state()["btc"]) == D(".01")


def test_normal_exit_cancels_then_taker_sells_only_remainder(paper):
    store, _, engine = paper
    enter(engine)
    engine.tick(quote(86405, "98", "99", "1000"), 86405)
    original = D(store.state()["btc"])
    engine.tick(quote(172800), 172800, {"action": "exit", "timestamp": 172800})
    engine.tick(quote(172805, "102", "103", "1"), 172805)
    assert D(store.state()["btc"]) == original - D(".01")
    engine.tick(quote(172860), 172860)
    assert D(store.state()["btc"]) == 0
    sells = [
        json.loads(r["body"])
        for r in store.rows("fills")
        if json.loads(r["body"])["side"] == "SELL"
    ]
    assert sum((D(f["quantity"]) for f in sells), D(0)) == original
    assert {f["liquidity"] for f in sells} == {"maker", "taker"}
    assert engine.reconcile(172865)


def test_halt_is_durable_before_partial_liquidation_and_manual_resume(paper):
    store, config, engine = paper
    enter(engine)
    engine.tick(quote(86405, "98", "99", "1000"), 86405)
    held = D(store.state()["btc"])
    engine.tick(quote(86410, "20", "21", "1"), 86410)
    assert store.state()["halted"]
    assert D(store.state()["btc"]) == held
    restarted = PaperEngine(Store(config.database), config, Product())
    assert restarted.reconcile(86415)
    restarted.tick(quote(86415, "20", "21", "1"), 86415)
    assert D(store.state()["btc"]) == held - 1
    assert store.state()["halted"] and store.state()["liquidating"]
    restarted.tick(quote(86420, "20", "21", "100"), 86420)
    assert D(store.state()["btc"]) == 0
    store.request_control("resume")
    restarted.tick(quote(86425, "20", "21"), 86425)
    assert store.rows("controls")[0]["status"] == "rejected"
    assert store.state()["peak"] == "500"
    store.request_control("reset_peak")
    restarted.tick(quote(86430, "20", "21"), 86430)
    assert store.state()["halted"]
    store.request_control("resume")
    restarted.tick(quote(86435, "20", "21"), 86435)
    assert not store.state()["halted"]


def test_uncertain_order_or_balance_change_blocks_reconciliation(paper):
    store, _, engine = paper
    enter(engine)
    with store.transaction() as db:
        order = orders(store)[0]
        order["status"] = "uncertain"
        db.execute("UPDATE orders SET body=? WHERE id=?", (encode(order), order["id"]))
    assert not engine.reconcile(86405)
    engine.tick(quote(86405, "98", "99"), 86405)
    assert not store.rows("fills")


def test_unexplained_cash_is_not_performance(paper):
    store, _, engine = paper
    with store.transaction() as db:
        state = store.state(db)
        state["cash"] = "501"
        store.save_state(db, state)
    assert not engine.reconcile(1)
    assert store.state()["health"] == "accounting_mismatch"


def test_fill_transaction_rolls_back_after_failure(paper, monkeypatch):
    store, _, engine = paper
    enter(engine)
    original = store.event

    def fail(db, kind, body, now=None):
        if kind == "fill":
            raise RuntimeError("simulated crash before commit")
        return original(db, kind, body, now)

    monkeypatch.setattr(store, "event", fail)
    with pytest.raises(RuntimeError):
        engine.tick(quote(86405, "98", "99"), 86405)
    assert not store.rows("fills")
    assert D(store.state()["cash"]) == 500
    assert engine.reconcile(86410)


def test_duplicate_worker_prevented(paper):
    store, _, _ = paper
    with WorkerLock(store):
        with pytest.raises(RuntimeError, match="Another worker"):
            with WorkerLock(store):
                pass


def test_backup_restores_ledger_and_halt(paper, tmp_path):
    store, config, engine = paper
    enter(engine)
    engine.tick(quote(86405, "98", "99", "1000"), 86405)
    engine.tick(quote(86410, "20", "21"), 86410)
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    restored = Store(backup)
    restored.initialize(config)
    assert restored.state()["halted"]
    assert PaperEngine(restored, config, Product()).reconcile(86415)
    with restored.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
