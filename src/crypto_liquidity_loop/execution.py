"""Persistent paper execution. This module has no authenticated exchange client."""

import json
import uuid

from .accounting import apply_fill, mark
from .domain import D, drawdown, equity, floor_step
from .storage import encode

ACTIVE = {"pending_submission", "open", "partial", "cancel_pending", "uncertain"}


class PaperEngine:
    def __init__(self, store, config, product):
        self.store, self.config, self.product = store, config, product

    def _orders(self, db):
        return [json.loads(r[0]) for r in db.execute("SELECT body FROM orders ORDER BY rowid")]

    def _save(self, db, order):
        db.execute("INSERT OR REPLACE INTO orders VALUES(?,?)", (order["id"], encode(order)))

    def _cancel(self, db, order, now, reason):
        if order["status"] not in ACTIVE:
            return
        order["status"] = "cancel_pending"
        self._save(db, order)
        self.store.event(db, "cancellation_requested", {"id": order["id"], "reason": reason}, now)
        # Paper exchange state is local and deterministic; no external cancellation race.
        order["status"] = "canceled"
        self._save(db, order)
        self.store.event(db, "cancellation_reconciled", {"id": order["id"]}, now)

    def _place(self, db, side, quantity, price, now, deadline, kind="limit"):
        order = {
            "id": str(uuid.uuid4()),
            "mode": "paper",
            "side": side,
            "quantity": str(quantity),
            "filled": "0",
            "price": str(price),
            "created_at": now,
            "deadline": deadline,
            "kind": kind,
            "status": "pending_submission",
            "last_fill_quote": None,
        }
        self._save(db, order)
        self.store.event(db, "order_intent", order, now)
        order["status"] = "open"
        self._save(db, order)
        return order

    def _fill(self, db, state, order, quantity, price, now, maker):
        rate = self.config.maker_fee if maker else self.config.taker_fee
        fee = apply_fill(state, order["side"], quantity, price, rate)
        order["filled"] = str(D(order["filled"]) + quantity)
        order["status"] = "filled" if D(order["filled"]) == D(order["quantity"]) else "partial"
        self._save(db, order)
        fill = {
            "side": order["side"],
            "quantity": str(quantity),
            "price": str(price),
            "fee": str(fee),
            "liquidity": "maker" if maker else "taker",
            "mode": "paper",
        }
        fill_id = str(uuid.uuid4())
        db.execute("INSERT INTO fills VALUES(?,?,?,?)", (fill_id, order["id"], now, encode(fill)))
        self.store.event(db, "fill", {"id": fill_id, **fill}, now)
        self.store.alert(
            db,
            f"fill:{order['id']}",
            "Paper order fill",
            f"Simulated {order['side']} {quantity} BTC at ${price}; fee ${fee}.",
            now,
        )

    def reconcile(self, now):
        with self.store.transaction() as db:
            state = self.store.state(db)
            ledger = {"cash": str(self.config.initial_cash), "btc": "0", "fees": "0"}
            for row in db.execute("SELECT body FROM fills ORDER BY rowid"):
                f = json.loads(row[0])
                qty, price, fee = D(f["quantity"]), D(f["price"]), D(f["fee"])
                cash, btc = D(ledger["cash"]), D(ledger["btc"])
                if f["side"] == "BUY":
                    cash -= qty * price + fee
                    btc += qty
                elif f["side"] == "SELL":
                    cash += qty * price - fee
                    btc -= qty
                else:
                    raise ValueError("Invalid persisted fill side")
                ledger.update(cash=str(cash), btc=str(btc), fees=str(D(ledger["fees"]) + fee))
            if any(D(state[k]) != D(ledger[k]) for k in ("cash", "btc", "fees")):
                state.update(reconciled=False, health="accounting_mismatch")
                self.store.save_state(db, state)
                self.store.event(db, "reconciliation_failed", {}, now)
                return False
            orders = self._orders(db)
            active = [o for o in orders if o["status"] in ACTIVE]
            uncertain = any(
                o["status"] in {"pending_submission", "cancel_pending", "uncertain"} for o in active
            )
            if uncertain or len(active) > 1:
                state.update(reconciled=False, health="uncertain_orders")
                self.store.save_state(db, state)
                return False
            for order in orders:
                total = sum(
                    (
                        D(json.loads(r[0])["quantity"])
                        for r in db.execute(
                            "SELECT body FROM fills WHERE order_id=?", (order["id"],)
                        )
                    ),
                    D(0),
                )
                if total != D(order["filled"]) or total > D(order["quantity"]):
                    state.update(reconciled=False, health="order_fill_mismatch")
                    self.store.save_state(db, state)
                    return False
            state.update(reconciled=True, reconciled_at=now)
            self.store.save_state(db, state)
            self.store.event(db, "reconciled", {"orders": len(active)}, now)
            return True

    def _controls(self, db, state, quote, now):
        for row in db.execute(
            "SELECT * FROM controls WHERE status='pending' ORDER BY id"
        ).fetchall():
            action, error = row["action"], None
            fresh = quote is not None and quote.fresh(now, self.config.quote_max_age_seconds)
            if action == "pause":
                state["paused"] = True
                for order in self._orders(db):
                    if order["side"] == "BUY":
                        self._cancel(db, order, now, "owner_pause")
            elif action == "liquidate":
                state.update(halted=True, paused=True, liquidating=True, halt_reason="owner")
                for order in self._orders(db):
                    self._cancel(db, order, now, "owner_liquidation")
            elif not fresh or not state["reconciled"]:
                error = "Fresh market data and successful reconciliation required"
            elif action == "resume":
                if state["liquidating"] and D(state["btc"]) > 0:
                    error = "Liquidation remains incomplete"
                elif (
                    drawdown(equity(state, quote.bid), D(state["peak"]))
                    >= self.config.drawdown_limit
                ):
                    error = (
                        "Drawdown still meets the halt threshold; reset peak separately if intended"
                    )
                else:
                    state.update(halted=False, paused=False, liquidating=False, halt_reason=None)
            elif action == "reset_peak":
                state["peak"] = str(equity(state, quote.bid))
            db.execute(
                "UPDATE controls SET status=?,result=? WHERE id=?",
                ("rejected" if error else "done", error or "Applied", row["id"]),
            )
            self.store.event(db, "control_processed", {"action": action, "error": error}, now)

    def tick(self, quote, now, decision=None, connectivity=None):
        with self.store.transaction() as db:
            state = self.store.state(db)
            if connectivity is not None:
                state["connectivity"] = connectivity
            was_halted = state["halted"]
            self._controls(db, state, quote, now)
            fresh = quote is not None and quote.fresh(now, self.config.quote_max_age_seconds)
            state["heartbeat"] = now
            if not fresh:
                state["health"] = "stale_data"
                self.store.alert(
                    db,
                    "stale",
                    "Market data unavailable",
                    "Paper fills and entries blocked; awaiting a fresh Coinbase quote.",
                    now,
                )
                # Deadlines still run while disconnected. A stale quote must never fill an order.
                for order in self._orders(db):
                    if order["status"] in ACTIVE and now >= order["deadline"]:
                        self._cancel(db, order, now, "deadline_without_quote")
                        if order["side"] == "SELL":
                            state["normal_exit_pending"] = True
                self.store.save_state(db, state)
                return
            if state.get("health") == "stale_data":
                self.store.alert(
                    db,
                    "recovery",
                    "Market data recovered",
                    "Fresh Coinbase quotes are available again.",
                    now,
                )
            state["quote"] = {
                "bid": str(quote.bid),
                "ask": str(quote.ask),
                "timestamp": quote.timestamp,
            }
            state["health"] = "healthy" if state["reconciled"] else "reconciliation_required"
            value, dd = mark(state, quote.bid)
            if dd >= self.config.drawdown_limit and not state["halted"]:
                state.update(halted=True, paused=True, liquidating=True, halt_reason="drawdown")
                for order in self._orders(db):
                    self._cancel(db, order, now, "drawdown")
                self.store.event(
                    db, "drawdown_halt", {"equity": str(value), "drawdown": str(dd)}, now
                )
                self.store.alert(
                    db,
                    "halt",
                    "Paper drawdown halt",
                    f"Drawdown {dd:.2%}. Halt persisted; liquidation pending.",
                    now,
                )
            newly_halted = state["halted"] and not was_halted
            # Commit the durable halt before a subsequent tick can liquidate.
            if state["reconciled"] and not newly_halted:
                self._execute(db, state, quote, now)
            if decision is not None and state["reconciled"]:
                self._decision(db, state, quote, now, decision)
            value, dd = mark(state, quote.bid)
            operating = (
                self.config.monthly_operating_cost
                * D(str(max(0, now - state["started_at"])))
                / D("2629800")
            )
            benchmark = None
            if state["benchmark_btc"] is not None:
                benchmark = D(state["benchmark_cash"]) + D(state["benchmark_btc"]) * quote.bid
            benchmark_liquidation = None
            if benchmark is not None:
                benchmark_liquidation = D(state["benchmark_cash"]) + D(
                    state["benchmark_btc"]
                ) * quote.bid * (1 - self.config.taker_fee) * (1 - self.config.slippage_bps / 10000)
            valuation = {
                "equity": str(value),
                "drawdown": str(dd),
                "btc": state["btc"],
                "cash": state["cash"],
                "liquidation_value": str(
                    D(state["cash"])
                    + D(state["btc"])
                    * quote.bid
                    * (1 - self.config.taker_fee)
                    * (1 - self.config.slippage_bps / 10000)
                ),
                "benchmark": str(benchmark) if benchmark is not None else None,
                "benchmark_liquidation_value": str(benchmark_liquidation)
                if benchmark_liquidation is not None
                else None,
                "benchmark_after_same_operating_cost": str(benchmark - operating)
                if benchmark is not None
                else None,
                "operating_cost": str(operating),
                "after_operating_cost": str(value - operating),
            }
            state["valuation"] = valuation
            db.execute("INSERT OR REPLACE INTO valuations VALUES(?,?)", (now, encode(valuation)))
            self.store.save_state(db, state)

    def _execute(self, db, state, quote, now):
        if state["liquidating"] or state.get("normal_exit_pending"):
            for order in self._orders(db):
                self._cancel(db, order, now, "liquidation")
            self._market_sell(db, state, quote, now)
            if D(state["btc"]) == 0:
                state["liquidating"] = False
                state["normal_exit_pending"] = False
            return
        for order in self._orders(db):
            if order["status"] not in ACTIVE:
                continue
            if now >= order["deadline"]:
                self._cancel(db, order, now, "deadline")
                if order["side"] == "SELL":
                    state["normal_exit_pending"] = True
                    self._market_sell(db, state, quote, now)
                    if D(state["btc"]) == 0:
                        state["normal_exit_pending"] = False
                continue
            if (
                state["halted"]
                or quote.timestamp <= order["created_at"]
                or (
                    order["last_fill_quote"] is not None
                    and quote.timestamp <= order["last_fill_quote"]
                )
            ):
                continue
            limit = D(order["price"])
            crossed = quote.ask < limit if order["side"] == "BUY" else quote.bid > limit
            if not crossed:
                continue
            size = quote.ask_size if order["side"] == "BUY" else quote.bid_size
            remaining = D(order["quantity"]) - D(order["filled"])
            quantity = floor_step(
                min(remaining, size * self.config.volume_participation), self.product.base_increment
            )
            if quantity > 0:
                order["last_fill_quote"] = quote.timestamp
                self._fill(db, state, order, quantity, limit, now, True)

    def _market_sell(self, db, state, quote, now):
        price = floor_step(
            quote.bid * (1 - self.config.slippage_bps / 10000), self.product.quote_increment
        )
        quantity = floor_step(min(D(state["btc"]), quote.bid_size), self.product.base_increment)
        if quantity == 0:
            return
        if not self.product.valid(quantity, price):
            self.store.alert(
                db,
                "dust",
                "Residual paper BTC",
                f"{state['btc']} BTC remains below executable minimums or available depth.",
                now,
            )
            return
        order = self._place(db, "SELL", quantity, price, now, now, "market")
        self._fill(db, state, order, quantity, price, now, False)

    def _decision(self, db, state, quote, now, decision):
        timestamp = decision["timestamp"]
        if timestamp is None or (state.get("last_signal") or 0) >= timestamp:
            return
        state["last_signal"] = timestamp
        state["signal"] = decision
        self.store.event(db, "strategy_decision", decision, now)
        if state["benchmark_btc"] is None:
            price = quote.ask * (1 + self.config.slippage_bps / 10000)
            quantity = self.product.buy_quantity(
                self.config.initial_cash, price, self.config.taker_fee
            )
            state.update(
                benchmark_btc=str(quantity),
                benchmark_cash=str(
                    self.config.initial_cash - quantity * price * (1 + self.config.taker_fee)
                ),
                benchmark_started_at=now,
            )
        if state["halted"] or state["liquidating"] or state.get("normal_exit_pending"):
            return
        if any(o["status"] in ACTIVE for o in self._orders(db)):
            return
        if decision["action"] == "enter" and D(state["btc"]) == 0 and not state["paused"]:
            deadline = timestamp + self.config.entry_window_seconds
            if now >= deadline:
                self.store.event(db, "missed_entry", {"reason": "expired_signal"}, now)
                return
            price = floor_step(quote.bid, self.product.quote_increment)
            quantity = self.product.buy_quantity(D(state["cash"]), price, self.config.maker_fee)
            if self.product.valid(quantity, price):
                self._place(db, "BUY", quantity, price, now, deadline)
        elif decision["action"] == "exit" and D(state["btc"]) > 0:
            # Round up for an ask-side post-only sell.
            step = self.product.quote_increment
            price = -floor_step(-quote.ask, step)
            if price < quote.ask:
                price += step
            quantity = floor_step(D(state["btc"]), self.product.base_increment)
            if self.product.valid(quantity, price):
                self._place(db, "SELL", quantity, price, now, now + self.config.exit_window_seconds)
