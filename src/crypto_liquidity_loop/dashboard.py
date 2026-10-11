import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .domain import DAY, D
from .research_conclusions import latest_reports, main_conclusion

ASSETS = Path(__file__).parent / "dashboard_assets"


def create_app(store, config):
    app = FastAPI(title="Crypto Liquidity Loop", docs_url=None, redoc_url=None, openapi_url=None)
    token = os.environ.get("LOOP_DASHBOARD_TOKEN", "")
    if token and len(token) < 32:
        raise ValueError("LOOP_DASHBOARD_TOKEN must have at least 32 characters")
    templates = Jinja2Templates(directory=str(ASSETS))
    app.mount("/static", StaticFiles(directory=str(ASSETS)), name="static")

    def sign(value):
        return hmac.new(token.encode(), value.encode(), hashlib.sha256).hexdigest()

    def session(request):
        cookie = request.cookies.get("loop_session", "")
        try:
            payload, signature = cookie.rsplit(".", 1)
            expiry, _ = payload.split(":", 1)
            if (
                token
                and float(expiry) > time.time()
                and hmac.compare_digest(signature, sign(payload))
            ):
                return payload
        except (ValueError, TypeError):
            pass
        return None

    def authorize(request):
        if token and not session(request):
            raise HTTPException(401, "Owner login required")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
            }
        )
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="login.html", context={"error": None, "enabled": bool(token)}
        )

    @app.post("/login")
    def login(request: Request, password: str = Form()):
        if not token or not hmac.compare_digest(password, token):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": "Invalid owner token", "enabled": bool(token)},
                status_code=401,
            )
        payload = f"{int(time.time() + 43200)}:{secrets.token_hex(16)}"
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "loop_session",
            f"{payload}.{sign(payload)}",
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=43200,
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, research_asset: str = "BTC"):
        if research_asset not in ("BTC", "ETH", "SOL"):
            raise HTTPException(400, "Unknown research asset")
        if token and not session(request):
            return RedirectResponse("/login", status_code=303)
        state = store.state()
        now = time.time()
        quote = state.get("quote") or {}
        fresh = bool(quote) and -2 <= now - quote["timestamp"] <= config.quote_max_age_seconds
        alive = now - state.get("heartbeat", 0) < 15
        health = state.get("health", "starting") if alive else "worker_offline"
        with store.connect() as db:
            count = db.execute("SELECT count(*) FROM valuations").fetchone()[0]
            stride = max(1, count // 500)
            rows = db.execute(
                "SELECT timestamp,body FROM valuations WHERE rowid % ?=0 ORDER BY timestamp",
                (stride,),
            ).fetchall()
        points = [json.loads(r["body"]) for r in rows]
        chart = chart_lines(points)
        report_rows = store.rows("reports", 1000)
        all_reports = latest_reports(report_rows)
        failures = []
        failed_keys = set()
        for row in report_rows:
            body = json.loads(row["body"])
            key = (body.get("product_id", "BTC-USD"), body["label"], body["start"], body["end"])
            if key in failed_keys:
                continue
            failed_keys.add(key)
            if body.get("status") == "incomplete" and body.get("asset") == research_asset:
                failures.append(body)
        reports = [r for r in all_reports if r["asset"] == research_asset]
        comparisons = [
            next(
                (
                    r
                    for r in all_reports
                    if r["asset"] == asset and r["body"]["label"] == "held_out"
                ),
                None,
            )
            for asset in ("BTC", "ETH", "SOL")
        ]
        conclusions = main_conclusion(reports)
        orders = [json.loads(r["body"]) for r in store.rows("orders", 20)]
        fills = [{**r, "body": json.loads(r["body"])} for r in store.rows("fills", 20)]
        events = [{**r, "body": json.loads(r["body"])} for r in store.rows("events", 20)]
        alerts = store.rows("alerts", 10)
        email = (
            "configured"
            if all(
                os.environ.get(k)
                for k in ("LOOP_RESEND_API_KEY", "LOOP_EMAIL_FROM", "LOOP_EMAIL_TO")
            )
            else "unconfigured"
        )
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "state": state,
                "valuation": state.get("valuation", {}),
                "health": health,
                "fresh": fresh,
                "alive": alive,
                "quote": quote,
                "chart": chart,
                "reports": reports,
                "research_asset": research_asset,
                "comparisons": comparisons,
                "research_failures": failures,
                "conclusions": conclusions,
                "orders": orders,
                "fills": fills,
                "events": events,
                "alerts": alerts,
                "email": email,
                "controls": store.rows("controls", 10),
                "control_enabled": bool(session(request)),
                "csrf": sign("csrf:" + session(request)) if session(request) else "",
                "next_evaluation": datetime.fromtimestamp(
                    (int(now) // DAY + 1) * DAY, timezone.utc
                ).isoformat(),
                "money": money,
                "percent": percent,
                "when": when,
            },
        )

    @app.get("/api/state")
    def api_state(request: Request):
        authorize(request)
        return JSONResponse(store.state())

    @app.get("/reports/{report_id}")
    def report(request: Request, report_id: str):
        authorize(request)
        with store.connect() as db:
            row = db.execute("SELECT body FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        return JSONResponse(
            json.loads(row[0]),
            headers={"Content-Disposition": f'attachment; filename="backtest-{report_id}.json"'},
        )

    @app.post("/controls/{action}")
    def control(request: Request, action: str, csrf: str = Form(), confirmed: str = Form("")):
        payload = session(request)
        if not payload or not hmac.compare_digest(csrf, sign("csrf:" + payload)):
            raise HTTPException(403, "Owner session and valid CSRF token required")
        if action not in ("pause", "resume", "liquidate", "reset_peak"):
            raise HTTPException(400, "Unsupported control")
        if action in ("liquidate", "reset_peak") and confirmed != "yes":
            raise HTTPException(400, "Explicit confirmation required")
        store.request_control(action)
        return RedirectResponse("/#controls", status_code=303)

    return app


def money(value):
    return f"${D(str(value)):,.2f}" if value is not None else "—"


def percent(value):
    return f"{D(str(value)):.2%}" if value is not None else "—"


def when(value):
    return (
        datetime.fromtimestamp(float(value), timezone.utc).strftime("%b %d, %H:%M:%S UTC")
        if value
        else "—"
    )


def chart_lines(points):
    if not points:
        return None
    values = [float(p[k]) for p in points for k in ("equity", "benchmark") if p.get(k) is not None]
    low, high = min(values), max(values)
    padding = max(1, (high - low) * 0.15)
    low, high = low - padding, high + padding

    def line(key):
        return " ".join(
            f"{i / max(1, len(points) - 1) * 900:.1f},{230 - (float(p[key]) - low) / (high - low) * 220:.1f}"
            for i, p in enumerate(points)
            if p.get(key) is not None
        )

    return {
        "strategy": line("equity"),
        "benchmark": line("benchmark"),
        "low": money(low),
        "high": money(high),
        "samples": len(points),
    }
