from .domain import D, drawdown, equity


def apply_fill(state, side, quantity, price, fee_rate):
    cash, btc = D(state["cash"]), D(state["btc"])
    if not all(v.is_finite() for v in (cash, btc, quantity, price, fee_rate)):
        raise ValueError("Nonfinite accounting value")
    fee = quantity * price * fee_rate
    if quantity <= 0 or price <= 0 or not 0 <= fee_rate < 1:
        raise ValueError("Invalid fill")
    if side == "BUY":
        cash -= quantity * price + fee
        btc += quantity
    elif side == "SELL":
        cash += quantity * price - fee
        btc -= quantity
    else:
        raise ValueError("Invalid fill side")
    if cash < 0 or btc < 0:
        raise ValueError("Fill exceeds available funds or BTC")
    state.update(cash=str(cash), btc=str(btc), fees=str(D(state.get("fees", "0")) + fee))
    return fee


def mark(state, bid):
    value = equity(state, bid)
    peak = max(D(state["peak"]), value)
    state["peak"] = str(peak)
    return value, drawdown(value, peak)
