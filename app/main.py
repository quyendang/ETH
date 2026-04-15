import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="QAPI Crypto Service")
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/bots", tags=["bots"])
scheduler = BackgroundScheduler(timezone="UTC")

# ENV
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.getenv("PUSHOVER_USER", "")
PUSHOVER_DEVICE = os.getenv("PUSHOVER_DEVICE", "")

TRACKED_SYMBOLS = ["ETHUSDT", "BTCUSDT"]
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
TRACKER_INTERVAL = os.getenv("TRACKER_INTERVAL", "4h")

ETH_RSI_SELL = float(os.getenv("ETH_RSI_SELL", "65"))
ETH_RSI_BUY = float(os.getenv("ETH_RSI_BUY", "40"))
MACD_FAST = int(os.getenv("ETH_MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("ETH_MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("ETH_MACD_SIGNAL", "9"))
TRACKER_CHECK_MINUTES = int(os.getenv("TRACKER_CHECK_MINUTES", "10"))

_rsi_last_values: Dict[str, Dict[str, Dict[str, float]]] = {}
_rsi_last_state: Dict[str, Dict[str, str]] = {sym: {TRACKER_INTERVAL: "unknown"} for sym in TRACKED_SYMBOLS}
_rsi_last_run: float = 0.0


def _rsi_fetch_klines(symbol: str, interval: str, limit: int = 200):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _rsi_wilder(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        raise ValueError("Not enough data to compute RSI")

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _rsi_latest(symbol: str, interval: str, period: int):
    kl = _rsi_fetch_klines(symbol, interval, limit=max(200, period * 5))
    closes = [float(k[4]) for k in kl]
    return closes[-1], _rsi_wilder(closes, period=period)


def _compute_ema_series(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period:
        raise ValueError(f"Not enough data for EMA({period})")
    ema_values: List[Optional[float]] = [None] * len(values)
    sma = sum(values[:period]) / period
    ema_values[period - 1] = sma
    k = 2 / (period + 1)
    ema_prev = sma
    for i in range(period, len(values)):
        ema = (values[i] - ema_prev) * k + ema_prev
        ema_values[i] = ema
        ema_prev = ema
    return ema_values


def _compute_macd_series(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    if len(closes) < slow + signal + 5:
        n = len(closes)
        return [0.0] * n, [0.0] * n, [0.0] * n

    ema_fast = _compute_ema_series(closes, fast)
    ema_slow = _compute_ema_series(closes, slow)

    macd_series: List[float] = []
    for ef, es in zip(ema_fast, ema_slow):
        macd_series.append(0.0 if ef is None or es is None else ef - es)

    signal_series = _compute_ema_series(macd_series, signal)
    hist_series: List[float] = []
    for m, s in zip(macd_series, signal_series):
        hist_series.append(0.0 if s is None else m - s)

    return macd_series, signal_series, hist_series


def _macd_latest_with_prev(symbol: str, interval: str):
    kl = _rsi_fetch_klines(symbol, interval, limit=max(200, MACD_SLOW * 5))
    closes = [float(k[4]) for k in kl]
    macd, signal, _ = _compute_macd_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if len(macd) < 2 or len(signal) < 2:
        raise ValueError("Not enough data to compute MACD latest")
    hist = macd[-1] - (signal[-1] or 0.0)
    prev_hist = macd[-2] - (signal[-2] or 0.0)
    return macd[-1], (signal[-1] or 0.0), hist, prev_hist


def _compute_eth_zones_from_range(symbol: str, interval: str, lookback: int = 60):
    kl = _rsi_fetch_klines(symbol, interval, limit=lookback)
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    recent_high = max(highs)
    recent_low = min(lows)
    price_range = recent_high - recent_low
    if price_range <= 0:
        raise ValueError("Invalid price range")

    zone_pct = 0.2
    buy_low = recent_low
    buy_high = recent_low + zone_pct * price_range
    sell_high = recent_high
    sell_low = recent_high - zone_pct * price_range
    return sell_low, sell_high, buy_low, buy_high, recent_low, recent_high


def _compute_rsi_series(closes: List[float], period: int) -> List[float]:
    if len(closes) < period + 2:
        return [50.0] * len(closes)

    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    def ema(series, p):
        alpha = 2 / (p + 1)
        vals = []
        prev = sum(series[:p]) / p
        vals.append(prev)
        for v in series[p:]:
            prev = alpha * v + (1 - alpha) * prev
            vals.append(prev)
        return vals

    avg_gain = ema(gains, period)
    avg_loss = ema(losses, period)
    rsi = [50.0] * len(closes)
    offset = len(closes) - len(avg_gain)

    for i in range(len(avg_gain)):
        if avg_loss[i] == 0:
            rsi[offset + i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            rsi[offset + i] = 100 - (100 / (1 + rs))

    return rsi


def _sma_series(values: List[float], period: int):
    n = len(values)
    if n < period:
        return [None] * n
    out = [None] * (period - 1)
    wsum = sum(values[:period])
    out.append(wsum / period)
    for i in range(period, n):
        wsum += values[i] - values[i - period]
        out.append(wsum / period)
    return out


def _bollinger_bands(values: List[float], period: int = 20, k: float = 2.0):
    import math

    n = len(values)
    middle = _sma_series(values, period)
    upper = [None] * n
    lower = [None] * n

    if n < period:
        return middle, upper, lower

    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        m = middle[i]
        if m is None:
            continue
        std = math.sqrt(sum((v - m) ** 2 for v in window) / period)
        upper[i] = m + k * std
        lower[i] = m - k * std

    return middle, upper, lower


def _stochastic_oscillator(highs: List[float], lows: List[float], closes: List[float], period: int = 14):
    n = len(closes)
    if n < period:
        return [None] * n

    out = [None] * n
    for i in range(period - 1, n):
        h = max(highs[i - period + 1 : i + 1])
        l = min(lows[i - period + 1 : i + 1])
        out[i] = 50.0 if h == l else (closes[i] - l) / (h - l) * 100.0
    return out


def _williams_r(highs: List[float], lows: List[float], closes: List[float], period: int = 14):
    n = len(closes)
    if n < period:
        return [None] * n

    out = [None] * n
    for i in range(period - 1, n):
        h = max(highs[i - period + 1 : i + 1])
        l = min(lows[i - period + 1 : i + 1])
        out[i] = -50.0 if h == l else -100.0 * (h - closes[i]) / (h - l)
    return out


def _pushover_notify(title: str, message: str):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        return
    data = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message,
        "priority": 0,
        "sound": "cash",
    }
    if PUSHOVER_DEVICE:
        data["device"] = PUSHOVER_DEVICE

    try:
        requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=15)
    except Exception:
        pass


def _eth_decide_action(
    price: float,
    rsi_h4: float,
    macd_hist: float,
    prev_macd_hist: float,
    zones: tuple,
    btc_rsi_h4: float,
    btc_macd_hist: float,
    btc_prev_macd_hist: float,
):
    sell_low, sell_high, buy_low, buy_high, recent_low, recent_high = zones
    reasons: List[str] = [
        f"Dynamic zones: BUY[{buy_low:.1f}-{buy_high:.1f}] SELL[{sell_low:.1f}-{sell_high:.1f}] (range {recent_low:.1f}-{recent_high:.1f})"
    ]
    action = "HOLD"

    macd_weakening = macd_hist > 0 and prev_macd_hist is not None and macd_hist < prev_macd_hist
    if sell_low <= price <= sell_high and rsi_h4 >= ETH_RSI_SELL and macd_weakening:
        action = "SELL"
        reasons.append(f"Price {price:.1f} in SELL zone & RSI_H4 {rsi_h4:.1f} >= {ETH_RSI_SELL}")
    elif buy_low <= price <= buy_high and rsi_h4 <= ETH_RSI_BUY:
        action = "BUY"
        reasons.append(f"Price {price:.1f} in BUY zone & RSI_H4 {rsi_h4:.1f} <= {ETH_RSI_BUY}")
    else:
        reasons.append("No buy/sell condition matched (HOLD).")

    btc_bull_rsi = btc_rsi_h4 >= 65
    btc_macd_stronger = btc_macd_hist > 0 and btc_prev_macd_hist is not None and btc_macd_hist >= btc_prev_macd_hist
    if action == "SELL" and (btc_bull_rsi or btc_macd_stronger):
        action = "HOLD"
        reasons.append(
            f"Cancel SELL: BTC still bullish (RSI_H4={btc_rsi_h4:.1f}, MACD hist {btc_macd_hist:.4f} >= prev {btc_prev_macd_hist:.4f})"
        )

    return {"action": action, "reason": " | ".join(reasons)}


def run_symbol_tracker_once(symbol: str, send_notify: bool = False):
    symbol = symbol.upper()

    price, rsi_h4 = _rsi_latest(symbol, TRACKER_INTERVAL, RSI_PERIOD)
    macd_line, macd_signal, macd_hist, prev_macd_hist = _macd_latest_with_prev(symbol, TRACKER_INTERVAL)

    btc_price, btc_rsi_h4 = _rsi_latest("BTCUSDT", TRACKER_INTERVAL, RSI_PERIOD)
    _, _, btc_macd_hist, btc_prev_macd_hist = _macd_latest_with_prev("BTCUSDT", TRACKER_INTERVAL)

    zones = _compute_eth_zones_from_range(symbol, TRACKER_INTERVAL, lookback=60)
    sell_low, sell_high, buy_low, buy_high, recent_low, recent_high = zones

    decision = _eth_decide_action(
        price, rsi_h4, macd_hist, prev_macd_hist, zones, btc_rsi_h4, btc_macd_hist, btc_prev_macd_hist
    )

    payload = {
        "symbol": symbol,
        "timeframe": TRACKER_INTERVAL,
        "now_utc": datetime.utcnow().isoformat() + "Z",
        "price": price,
        "rsi_h4": rsi_h4,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "action": decision["action"],
        "reason": decision["reason"],
        "zones": {
            "sell_low": sell_low,
            "sell_high": sell_high,
            "buy_low": buy_low,
            "buy_high": buy_high,
            "recent_low": recent_low,
            "recent_high": recent_high,
        },
        "btc": {
            "price": btc_price,
            "rsi_h4": btc_rsi_h4,
            "macd_hist": btc_macd_hist,
            "prev_macd_hist": btc_prev_macd_hist,
        },
    }

    if send_notify and payload["action"] != "HOLD":
        _pushover_notify(
            f"[{payload['action']}] {symbol}",
            f"Price: {price}\nReason: {payload['reason']}\nTime (UTC): {payload['now_utc']}",
        )

    return payload


def symbols_tracker_job():
    for symbol in TRACKED_SYMBOLS:
        try:
            payload = run_symbol_tracker_once(symbol, send_notify=True)
            logging.info("[SYMBOL_TRACKER_JOB] %s: action=%s price=%s", symbol, payload["action"], payload["price"])
        except Exception as e:
            logging.error("[SYMBOL_TRACKER_JOB] %s: %s", symbol, e)


def _rsi_check_once():
    global _rsi_last_run, _rsi_last_values

    snap: Dict[str, Dict[str, Dict[str, float]]] = {TRACKER_INTERVAL: {}}
    for sym in TRACKED_SYMBOLS:
        try:
            price, rsi = _rsi_latest(sym, TRACKER_INTERVAL, RSI_PERIOD)
            snap[TRACKER_INTERVAL][sym] = {"price": price, "rsi": rsi}
        except Exception as e:
            snap[TRACKER_INTERVAL][sym] = {"error": str(e)}

    _rsi_last_values = snap
    _rsi_last_run = datetime.utcnow().timestamp()


@router.get("/rsi-status")
def rsi_status():
    return {
        "symbols": TRACKED_SYMBOLS,
        "period": RSI_PERIOD,
        "timeframes": {TRACKER_INTERVAL: TRACKER_INTERVAL},
        "last_run_utc": datetime.utcfromtimestamp(_rsi_last_run).strftime("%Y-%m-%d %H:%M:%S") if _rsi_last_run else None,
        "values": _rsi_last_values,
        "state": _rsi_last_state,
        "check_every_minutes": TRACKER_CHECK_MINUTES,
    }


@router.get("/run/{symbol}")
def run_tracker(symbol: str):
    return run_symbol_tracker_once(symbol, send_notify=False)


async def _render_symbol_dashboard(request: Request, symbol: str):
    symbol = symbol.upper()

    try:
        klines = _rsi_fetch_klines(symbol, TRACKER_INTERVAL, limit=200)
    except Exception as e:
        logging.error("[SYMBOL DASH] Error fetching klines for %s: %s", symbol, e)
        klines = []

    labels: List[str] = []
    closes: List[float] = []
    highs: List[float] = []
    lows: List[float] = []

    for k in klines:
        try:
            dt = datetime.utcfromtimestamp(int(k[0]) / 1000.0)
            labels.append(dt.strftime("%Y-%m-%d %H:%M"))
            highs.append(float(k[2]))
            lows.append(float(k[3]))
            closes.append(float(k[4]))
        except Exception:
            continue

    if not closes:
        return templates.TemplateResponse(
            request,
            "symbol_dashboard.html",
            {
                "request": request,
                "symbol": symbol,
                "rows_json": [],
                "last_price": None,
                "last_rsi": None,
                "change_24h": None,
                "buy_low": None,
                "buy_high": None,
                "sell_low": None,
                "sell_high": None,
                "tracker_action": "HOLD",
                "tracker_reason": "No data",
            },
        )

    rsi_values = _compute_rsi_series(closes, RSI_PERIOD)
    _, _, macd_hist_values = _compute_macd_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    ema_fast = _compute_ema_series(closes, 12)
    ema_slow = _compute_ema_series(closes, 26)
    _, bb_upper, bb_lower = _bollinger_bands(closes, period=20, k=2.0)
    stoch_k = _stochastic_oscillator(highs, lows, closes, period=14)
    williams_r = _williams_r(highs, lows, closes, period=14)

    buy_low = buy_high = sell_low = sell_high = None
    try:
        sell_low, sell_high, buy_low, buy_high, _, _ = _compute_eth_zones_from_range(symbol, TRACKER_INTERVAL, lookback=60)
    except Exception as e:
        logging.error("[SYMBOL DASH] Error computing zones for %s: %s", symbol, e)

    min_len = min(
        len(closes),
        len(labels),
        len(rsi_values),
        len(macd_hist_values),
        len(ema_fast),
        len(ema_slow),
        len(bb_upper),
        len(bb_lower),
        len(stoch_k),
        len(williams_r),
    )

    labels = labels[-min_len:]
    closes = closes[-min_len:]
    rsi_values = rsi_values[-min_len:]
    macd_hist_values = macd_hist_values[-min_len:]
    ema_fast = ema_fast[-min_len:]
    ema_slow = ema_slow[-min_len:]
    bb_upper = bb_upper[-min_len:]
    bb_lower = bb_lower[-min_len:]
    stoch_k = stoch_k[-min_len:]
    williams_r = williams_r[-min_len:]

    rows_json: List[Dict[str, Any]] = []
    for i in range(min_len):
        rows_json.append(
            {
                "time_str": labels[i],
                "price": closes[i],
                "rsi_h4": rsi_values[i],
                "macd_hist": macd_hist_values[i],
                "ema_fast": ema_fast[i],
                "ema_slow": ema_slow[i],
                "bb_upper": bb_upper[i],
                "bb_lower": bb_lower[i],
                "stoch_k": stoch_k[i],
                "wr": williams_r[i],
            }
        )

    last_price = closes[-1]
    last_rsi = rsi_values[-1] if rsi_values else None
    change_24h = None
    if len(closes) >= 7 and closes[-7] != 0:
        change_24h = (last_price - closes[-7]) / closes[-7] * 100.0

    tracker_action = "HOLD"
    tracker_reason = ""
    try:
        payload = run_symbol_tracker_once(symbol, send_notify=False)
        tracker_action = payload.get("action", "HOLD")
        tracker_reason = payload.get("reason", "")
    except Exception as e:
        logging.error("[SYMBOL DASH] run_symbol_tracker_once failed for %s: %s", symbol, e)

    return templates.TemplateResponse(
        request,
        "symbol_dashboard.html",
        {
            "request": request,
            "symbol": symbol,
            "rows_json": rows_json,
            "last_price": last_price,
            "last_rsi": last_rsi,
            "change_24h": change_24h,
            "buy_low": buy_low,
            "buy_high": buy_high,
            "sell_low": sell_low,
            "sell_high": sell_high,
            "tracker_action": tracker_action,
            "tracker_reason": tracker_reason,
        },
    )


@app.get("/")
def home_redirect():
    return RedirectResponse(url="/ETHUSDT", status_code=307)


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "qapi-crypto",
        "time_utc": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/ETHUSDT", response_class=HTMLResponse)
async def ethusdt_dashboard(request: Request):
    return await _render_symbol_dashboard(request, "ETHUSDT")


@app.get("/BTCUSDT", response_class=HTMLResponse)
async def btcusdt_dashboard(request: Request):
    return await _render_symbol_dashboard(request, "BTCUSDT")


app.include_router(router)


@app.on_event("startup")
def on_startup():
    _rsi_check_once()
    scheduler.add_job(
        symbols_tracker_job,
        "interval",
        minutes=TRACKER_CHECK_MINUTES,
        id="symbols_tracker_job",
        replace_existing=True,
        next_run_time=datetime.utcnow(),
    )
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)
