
# ===============================
# main_refactored.py — FastAPI + Supabase + RSI Bot (ETHUSDT & BTCUSDT)
# ===============================
import os
import re
import time
import uuid
import base64
import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import (
    FastAPI,
    Query,
    Request,
    Form,
    HTTPException,
    Header,
    Depends,
    APIRouter,
)
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from supabase import create_client, Client


from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from cryptography.fernet import Fernet, InvalidToken

# ------------------------------------------------------------------
# 1) GLOBAL APP/ENV CONFIG
# ------------------------------------------------------------------
app = FastAPI()
templates = Jinja2Templates(directory="templates")
# thêm filter format số có dấu phẩy
def comma_format(value):
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return value

templates.env.filters["comma"] = comma_format


BASE_DIR = Path(__file__).resolve().parent
APP_ADS_PATH = BASE_DIR / "app-ads.txt"
APP_FAVICON_PATH = BASE_DIR / "favicon.ico"
logging.basicConfig(level=logging.INFO)

# Supabase ENV
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")

# Stable salt to derive UUIDv5 from incoming lessonid
SALT = "548efb19-9741-4e81-9ad1-dddbe062649d"

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL và SUPABASE_KEY phải được thiết lập trong biến môi trường.")

if not SUPABASE_SERVICE_KEY or not ADMIN_API_KEY:
    raise ValueError("SUPABASE_SERVICE_ROLE_KEY và ADMIN_API_KEY phải được thiết lập trong biến môi trường.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
supabase_admin: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def verify_api_key(x_api_key: str | None = Header(default=None)):
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


# ------------------------------------------------------------------
# 2) DATA MODELS
# ------------------------------------------------------------------
class ClearResult(BaseModel):
    status: str
    userid: str
    rpc_result: Dict[str, Any] | None = None
    auth_deleted: bool
    note: str | None = None


class UserCounts(BaseModel):
    groups: int
    lessons: int
    words: int


class UserWithCounts(BaseModel):
    id: str
    email: Optional[str] = None
    last_sign_in_at: Optional[str] = None
    counts: UserCounts


class UsersListResponse(BaseModel):
    page: int
    per_page: int
    total: int
    users: List[UserWithCounts]


class UserStatsResponse(BaseModel):
    userid: str
    email: str | None = None
    counts: Dict[str, int]


# --------- Helper: tạo Fernet key từ password + salt ---------
def get_fernet(password: str, salt: str) -> Fernet:
    """
    Tạo Fernet object từ password + salt (string).
    Salt nên cố định nếu muốn decode lại sau này.
    """
    password_bytes = password.encode("utf-8")
    salt_bytes = salt.encode("utf-8")

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt_bytes,
        iterations=390_000,
        backend=default_backend(),
    )
    key = base64.urlsafe_b64encode(kdf.derive(password_bytes))
    return Fernet(key)

# --------- Routes ---------



# ------------------------------------------------------------------
# 3) UTILS
# ------------------------------------------------------------------
def _decode_b64_csv_to_ints(b64text: str | None) -> list[int]:
    """
    Giải mã base64 (URL-safe) -> chuỗi CSV -> list[int].
    Trả về [] nếu trống/không hợp lệ.
    """
    if not b64text:
        return []
    try:
        padding = "=" * (-len(b64text) % 4)
        raw = base64.urlsafe_b64decode((b64text + padding).encode("utf-8")).decode("utf-8")
        return [int(x) for x in raw.split(",") if x.strip().isdigit()]
    except Exception as ex:
        logging.warning(f"[WARN] Invalid base64 '{b64text}': {ex}")
        return []


# ------------------------------------------------------------------
# 4) RSI BOT (Inline, Dual Symbols: ETHUSDT, BTCUSDT)
# ------------------------------------------------------------------
# ENV
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.getenv("PUSHOVER_USER", "")
PUSHOVER_DEVICE = os.getenv("PUSHOVER_DEVICE", "")  # optional
RSI_SYMBOLS = [s.strip() for s in os.getenv("RSI_SYMBOLS", "ETHUSDT,BTCUSDT").split(",") if s.strip()] or ["ETHUSDT", "BTCUSDT"]
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_CHECK_MINUTES = int(os.getenv("RSI_CHECK_MINUTES", "5"))
RSI_TIMEFRAMES = {"1h": "1h", "4h": "4h", "1d": "1d"}

# ------------------------------------------------------------------
# ETH TRACKER CONFIG (dùng cho /bot/ethtracker)
# ------------------------------------------------------------------
ETH_TRACKER_SYMBOL = os.getenv("ETH_TRACKER_SYMBOL", "ETHUSDT")
ETH_TRACKER_INTERVAL = os.getenv("ETH_TRACKER_INTERVAL", "4h")
ETH_CYCLE_SIZE = float(os.getenv("ETH_CYCLE_SIZE", "40"))   # số ETH bán/mua mỗi vòng
ETH_BASE_BALANCE = float(os.getenv("ETH_BASE_BALANCE", "138"))  # tổng ETH ban đầu

# Nếu chưa có: map interval -> milliseconds
BIG_ORDER_THRESHOLD = 100_000  # > 100k USDT
INTERVAL_MS_MAP = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}
TRACKER_INTERVAL = ETH_TRACKER_INTERVAL  # ví dụ "4h"

# Vùng giá bán / mua xoay vòng & ngưỡng RSI (có thể chỉnh qua env)
ETH_SELL_ZONE_LOW = float(os.getenv("ETH_SELL_ZONE_LOW", "3650"))
ETH_SELL_ZONE_HIGH = float(os.getenv("ETH_SELL_ZONE_HIGH", "3700"))
ETH_BUY_ZONE_LOW = float(os.getenv("ETH_BUY_ZONE_LOW", "3350"))
ETH_BUY_ZONE_HIGH = float(os.getenv("ETH_BUY_ZONE_HIGH", "3450"))
ETH_RSI_SELL = float(os.getenv("ETH_RSI_SELL", "65"))
ETH_RSI_BUY = float(os.getenv("ETH_RSI_BUY", "40"))

# MACD tham số chuẩn TradingView
MACD_FAST = int(os.getenv("ETH_MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("ETH_MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("ETH_MACD_SIGNAL", "9"))




# State
_rsi_last_state: Dict[str, Dict[str, str]] = {sym: {tf: "unknown" for tf in RSI_TIMEFRAMES} for sym in RSI_SYMBOLS}
_rsi_last_values: Dict[str, Dict[str, Dict[str, float]]] = {}
_rsi_last_run: float = 0.0

# Router
_rsi_router = APIRouter()


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


def _rsi_fetch_klines(symbol: str, interval: str, limit: int = 200):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

def _compute_eth_zones_from_range(symbol: str, interval: str, lookback: int = 60):
    """
    Tính vùng BUY/SELL zone dựa trên high/low của N cây H4 gần nhất.
    - lookback: số nến dùng để tính (vd 60 nến H4 ≈ 10 ngày)
    Trả về: (sell_low, sell_high, buy_low, buy_high, recent_low, recent_high)
    """
    kl = _rsi_fetch_klines(symbol, interval, limit=lookback)
    if len(kl) < lookback:
        raise ValueError("Not enough klines for dynamic zone calc")

    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]

    recent_high = max(highs)
    recent_low = min(lows)
    price_range = recent_high - recent_low

    # Nếu range quá nhỏ thì tránh cho bot trade linh tinh
    if price_range <= 0:
        raise ValueError("Invalid price range for ETH")

    # Ví dụ: top/bottom 20% của range
    zone_pct = 0.2

    buy_low = recent_low
    buy_high = recent_low + zone_pct * price_range

    sell_high = recent_high
    sell_low = recent_high - zone_pct * price_range

    return sell_low, sell_high, buy_low, buy_high, recent_low, recent_high



def _compute_ema_series(values: List[float], period: int) -> List[Optional[float]]:
    """
    Trả về list EMA cùng độ dài với values.
    Các phần tử đầu (chưa đủ period) sẽ là None.
    """
    if len(values) < period:
        raise ValueError(f"Not enough data for EMA({period})")

    ema_values: List[Optional[float]] = [None] * len(values)
    # EMA đầu = SMA
    sma = sum(values[:period]) / period
    ema_values[period - 1] = sma

    k = 2 / (period + 1)
    ema_prev = sma
    for i in range(period, len(values)):
        ema = (values[i] - ema_prev) * k + ema_prev
        ema_values[i] = ema
        ema_prev = ema

    return ema_values


def _macd_latest(symbol: str, interval: str, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL):
    """
    Tính MACD (fast, slow, signal) cho symbol/interval.
    Trả về (macd_line, signal_line, hist) cho cây nến mới nhất.
    """
    # lấy nhiều dữ liệu 1 chút cho mượt
    limit = max(200, slow * 5)
    kl = _rsi_fetch_klines(symbol, interval, limit=limit)
    closes = [float(k[4]) for k in kl]

    if len(closes) < slow + signal + 5:
        raise ValueError("Not enough data to compute MACD")

    ema_fast = _compute_ema_series(closes, fast)
    ema_slow = _compute_ema_series(closes, slow)

    # MACD series = EMA_fast - EMA_slow
    macd_series: List[float] = []
    for ef, es in zip(ema_fast, ema_slow):
        if ef is None or es is None:
            macd_series.append(0.0)
        else:
            macd_series.append(ef - es)

    # EMA signal trên macd_series
    signal_series = _compute_ema_series(macd_series, signal)

    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
    if signal_line is None:
        raise ValueError("Signal line not ready")

    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def _macd_latest_with_prev(
    symbol: str,
    interval: str,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
):
    """
    Tính MACD cho symbol/interval.
    Trả về (macd_line, signal_line, hist, prev_hist):
    - hist: histogram cây hiện tại
    - prev_hist: histogram cây liền trước
    """
    limit = max(200, slow * 5)
    kl = _rsi_fetch_klines(symbol, interval, limit=limit)
    closes = [float(k[4]) for k in kl]

    if len(closes) < slow + signal + 5:
        raise ValueError("Not enough data to compute MACD")

    ema_fast = _compute_ema_series(closes, fast)
    ema_slow = _compute_ema_series(closes, slow)

    macd_series: List[float] = []
    for ef, es in zip(ema_fast, ema_slow):
        if ef is None or es is None:
            macd_series.append(0.0)
        else:
            macd_series.append(ef - es)

    signal_series = _compute_ema_series(macd_series, signal)

    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
    prev_signal_line = signal_series[-2]

    if signal_line is None or prev_signal_line is None:
        raise ValueError("Signal line not ready")

    hist = macd_line - signal_line
    prev_hist = macd_series[-2] - prev_signal_line

    return macd_line, signal_line, hist, prev_hist




def _rsi_latest(symbol: str, interval: str, period: int):
    kl = _rsi_fetch_klines(symbol, interval, limit=max(200, period * 5))
    closes = [float(k[4]) for k in kl]
    rsi = _rsi_wilder(closes, period=period)
    price = closes[-1]
    return price, rsi


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


def _fmt_dual(tf: str, condition: str, snapshot: Dict[str, Dict[str, float]]):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + "Z"
    lines = [f"TF: {tf} | Cond: {condition} | RSI({RSI_PERIOD}) | {ts}"]
    ordered = sorted(snapshot.items(), key=lambda kv: (0 if kv[0].upper() == "ETHUSDT" else 1, kv[0]))
    for sym, v in ordered:
        if "price" in v and "rsi" in v:
            lines.append(f"{sym}: Price {v['price']:.2f} | RSI {v['rsi']:.2f}")
        else:
            lines.append(f"{sym}: error {v.get('error', 'unknown')}")
    return "\n".join(lines)


def _compute_rsi_series(closes: list[float], period: int) -> list[float]:
    """
    Tính RSI series classic từ list closes.
    Trả về list có cùng độ dài với closes (các giá trị đầu có thể bằng None -> thay bằng 50).
    """
    if len(closes) < period + 2:
        return [50.0] * len(closes)

    gains = []
    losses = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    # EMA cho gains/losses
    def ema(series, p):
        alpha = 2 / (p + 1)
        ema_vals = []
        prev = sum(series[:p]) / p
        ema_vals.append(prev)
        for v in series[p:]:
            prev = alpha * v + (1 - alpha) * prev
            ema_vals.append(prev)
        return ema_vals

    avg_gain = ema(gains, period)
    avg_loss = ema(losses, period)

    rsi = [50.0] * len(closes)
    # align index
    offset = len(closes) - len(avg_gain)
    for i in range(len(avg_gain)):
        if avg_loss[i] == 0:
            rs = float('inf')
            r = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            r = 100 - (100 / (1 + rs))
        rsi[offset + i] = r

    return rsi


def _compute_macd_series(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float], list[float], list[float]]:
    """
    Tính MACD series cho 1 list closes.
    Trả về (macd_line[], signal_line[], hist[])
    """
    if len(closes) < slow + signal + 5:
        n = len(closes)
        return [0.0]*n, [0.0]*n, [0.0]*n

    ema_fast = _compute_ema_series(closes, fast)
    ema_slow = _compute_ema_series(closes, slow)

    macd_series: list[float] = []
    for ef, es in zip(ema_fast, ema_slow):
        if ef is None or es is None:
            macd_series.append(0.0)
        else:
            macd_series.append(ef - es)

    signal_series = _compute_ema_series(macd_series, signal)
    hist_series: list[float] = []
    for m, s in zip(macd_series, signal_series):
        if s is None:
            hist_series.append(0.0)
        else:
            hist_series.append(m - s)

    return macd_series, signal_series, hist_series

def _sma_series(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    if n < period:
        return [None] * n
    out: list[float | None] = [None] * (period - 1)
    window_sum = sum(values[:period])
    out.append(window_sum / period)
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        out.append(window_sum / period)
    return out


def _bollinger_bands(values: list[float], period: int = 20, k: float = 2.0):
    """
    Trả về (middle[], upper[], lower[])
    middle = SMA(period)
    upper/lower = middle ± k * std
    """
    n = len(values)
    middle = _sma_series(values, period)
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n

    if n < period:
        return middle, upper, lower

    import math

    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        m = middle[i]
        if m is None:
            continue
        variance = sum((v - m) ** 2 for v in window) / period
        std = math.sqrt(variance)
        upper[i] = m + k * std
        lower[i] = m - k * std

    return middle, upper, lower


def _stochastic_oscillator(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """
    %K: 0..100
    """
    n = len(closes)
    if n < period:
        return [None] * n

    out: list[float | None] = [None] * n
    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1 : i + 1])
        window_low = min(lows[i - period + 1 : i + 1])
        if window_high == window_low:
            out[i] = 50.0
        else:
            out[i] = (closes[i] - window_low) / (window_high - window_low) * 100.0
    return out


def _williams_r(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """
    Williams %R: -100 .. 0
    """
    n = len(closes)
    if n < period:
        return [None] * n

    out: list[float | None] = [None] * n
    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1 : i + 1])
        window_low = min(lows[i - period + 1 : i + 1])
        if window_high == window_low:
            out[i] = -50.0
        else:
            out[i] = -100.0 * (window_high - closes[i]) / (window_high - window_low)
    return out


def _rsi_check_once():
    global _rsi_last_state, _rsi_last_values, _rsi_last_run
    snap_all: Dict[str, Dict[str, Dict[str, float]]] = {}

    for tf, interval in RSI_TIMEFRAMES.items():
        tf_snap: Dict[str, Dict[str, float]] = {}

        for sym in RSI_SYMBOLS:
            try:
                price, rsi = _rsi_latest(sym, interval, RSI_PERIOD)
                tf_snap[sym] = {"price": price, "rsi": rsi}
            except Exception as e:
                tf_snap[sym] = {"error": str(e)}  # keep error to show in status

        # transitions per symbol
        for sym in RSI_SYMBOLS:
            v = tf_snap.get(sym, {})
            rsi = v.get("rsi")
            if rsi is None:
                continue
            prev = _rsi_last_state.get(sym, {}).get(tf, "unknown")
            if rsi < 30 and prev != "oversold":
                _pushover_notify(f"RSI Oversold {tf} — {sym}", _fmt_dual(tf, "<30", tf_snap))
                _rsi_last_state[sym][tf] = "oversold"
            elif rsi > 70 and prev != "overbought":
                _pushover_notify(f"RSI Overbought {tf} — {sym}", _fmt_dual(tf, ">70", tf_snap))
                _rsi_last_state[sym][tf] = "overbought"
            elif 30 <= rsi <= 70 and prev != "normal":
                _rsi_last_state[sym][tf] = "normal"

        snap_all[tf] = tf_snap

    _rsi_last_values = snap_all
    _rsi_last_run = time.time()
    return snap_all
    
def _eth_decide_action(
    price: float,
    rsi_h4: float,
    macd_hist: float,
    prev_macd_hist: float,
    zones: tuple,
    btc_rsi_h4: float,
    btc_macd_hist: float,
    btc_prev_macd_hist: float,
) -> Dict[str, str]:
    """
    Quyết định BUY/SELL/HOLD với:
    - zones: (sell_low, sell_high, buy_low, buy_high, recent_low, recent_high)
    - BTC filter để tránh bán ngược trend.
    """
    sell_low, sell_high, buy_low, buy_high, recent_low, recent_high = zones

    reasons: List[str] = []
    reasons.append(
        f"Dynamic zones: BUY[{buy_low:.1f}-{buy_high:.1f}] "
        f"SELL[{sell_low:.1f}-{sell_high:.1f}] "
        f"(range {recent_low:.1f}-{recent_high:.1f})"
    )

    action = "HOLD"

    # MACD hist đang yếu đi? (đỉnh tròn)
    macd_weakening = (
        macd_hist > 0
        and prev_macd_hist is not None
        and macd_hist < prev_macd_hist
    )

    # Điều kiện SELL cơ bản
    if (
        sell_low <= price <= sell_high
        and rsi_h4 >= ETH_RSI_SELL
        and macd_weakening
    ):
        action = "SELL"
        reasons.append(
            f"Price {price:.1f} in SELL zone & RSI_H4 {rsi_h4:.1f} >= {ETH_RSI_SELL}"
        )
        reasons.append(
            f"MACD hist weakening: current {macd_hist:.4f} < prev {prev_macd_hist:.4f}"
        )

    # Điều kiện BUY
    elif buy_low <= price <= buy_high and rsi_h4 <= ETH_RSI_BUY:
        action = "BUY"
        reasons.append(
            f"Price {price:.1f} in BUY zone & RSI_H4 {rsi_h4:.1f} <= {ETH_RSI_BUY}"
        )

    else:
        reasons.append("No buy/sell condition matched (HOLD).")

    # ===== BTC FILTER: tránh bán ngược trend BTC =====
    btc_bull_rsi = btc_rsi_h4 >= 65
    btc_macd_stronger = (
        btc_macd_hist > 0
        and btc_prev_macd_hist is not None
        and btc_macd_hist >= btc_prev_macd_hist
    )

    if action == "SELL" and (btc_bull_rsi or btc_macd_stronger):
        reasons.append(
            f"Cancel SELL: BTC still bullish (RSI_H4={btc_rsi_h4:.1f}, "
            f"MACD hist {btc_macd_hist:.4f} >= prev {btc_prev_macd_hist:.4f})"
        )
        action = "HOLD"

    # Info thêm về MACD (generic, không còn chữ ETH)
    if abs(macd_hist) < 0.5:
        reasons.append("MACD hist ~0 → momentum weak / sideway.")
    elif macd_hist > 0:
        reasons.append("MACD hist > 0 → bullish momentum.")
    else:
        reasons.append("MACD hist < 0 → bearish momentum.")

    return {
        "action": action,
        "reason": " | ".join(reasons),
    }



def _get_next_cycle_index() -> int:
    resp = supabase_admin.table("eth_cycles") \
        .select("cycle_index") \
        .order("cycle_index", desc=True) \
        .limit(1) \
        .execute()
    data = resp.data or []
    if not data:
        return 1
    return int(data[0]["cycle_index"]) + 1


def _get_open_cycle():
    resp = supabase_admin.table("eth_cycles") \
        .select("*") \
        .is_("buy_price", None) \
        .order("cycle_index", desc=True) \
        .limit(1) \
        .execute()
    data = resp.data or []
    return data[0] if data else None

    

# ===== API ENDPOINT =====

def run_symbol_tracker_once(symbol: str, send_notify: bool = False) -> Dict[str, Any]:
    """
    Tracker chung cho mọi symbol:
    - Nếu symbol == ETH_TRACKER_SYMBOL:
        dùng run_eth_tracker_once (giữ nguyên logic cũ, vẫn ghi ethdata, eth_cycles,...).
    - Symbol khác:
        + Lấy price + RSI H4 từ Binance
        + MACD + prev hist
        + Dynamic zone (dùng logic _compute_eth_zones_from_range)
        + BTC filter (BTC RSI + MACD hist + prev hist)
        + Quyết định action bằng _eth_decide_action
        + Không ghi DB, chỉ trả payload (+ optional Pushover).
    """
    symbol = symbol.upper()

    # # ETH: dùng luôn logic cũ để không phá eth_dashboard, eth_cycles...
    # if symbol == ETH_TRACKER_SYMBOL:
    #     return run_eth_tracker_once(send_notify=send_notify)

    interval = TRACKER_INTERVAL

    # 1) Symbol Price + RSI H4
    price, rsi_h4 = _rsi_latest(symbol, interval, RSI_PERIOD)

    # 2) Symbol MACD + prev hist
    macd_line, macd_signal, macd_hist, prev_macd_hist = _macd_latest_with_prev(
        symbol,
        interval,
    )

    # 3) BTC Price + RSI H4
    btc_price, btc_rsi_h4 = _rsi_latest("BTCUSDT", interval, RSI_PERIOD)

    # 4) BTC MACD + prev hist
    _, _, btc_macd_hist, btc_prev_macd_hist = _macd_latest_with_prev(
        "BTCUSDT",
        interval,
    )

    # 5) Dynamic zones cho chính symbol (re-use logic ETH)
    zones = _compute_eth_zones_from_range(symbol, interval, lookback=60)
    sell_low, sell_high, buy_low, buy_high, recent_low, recent_high = zones

    # 6) Quyết định action (BUY/SELL/HOLD) với BTC filter
    decision = _eth_decide_action(
        price=price,
        rsi_h4=rsi_h4,
        macd_hist=macd_hist,
        prev_macd_hist=prev_macd_hist,
        zones=zones,
        btc_rsi_h4=btc_rsi_h4,
        btc_macd_hist=btc_macd_hist,
        btc_prev_macd_hist=btc_prev_macd_hist,
    )
    action = decision["action"]
    reason = decision["reason"]

    now_utc = datetime.utcnow().isoformat() + "Z"

    payload: Dict[str, Any] = {
        "symbol": symbol,
        "timeframe": interval,
        "now_utc": now_utc,
        "price": price,
        "rsi_h4": rsi_h4,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "action": action,
        "reason": reason,
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

    # 7) Notify Pushover nếu cần và action != HOLD
    if send_notify and action != "HOLD":
        try:
            title = f"[{action}] {symbol} 💰"
            msg_lines = [
                f"Price: {price}",
                f"Reason: {reason}",
                f"RSI H4: {rsi_h4:.2f}",
                f"MACD: {macd_line:.4f} | Signal: {macd_signal:.4f} | Hist: {macd_hist:.4f}",
                f"BTC RSI H4: {btc_rsi_h4:.1f}, BTC hist: {btc_macd_hist:.4f}",
                f"Time (UTC): {now_utc}",
            ]
            _pushover_notify(title, "\n".join(msg_lines))
        except Exception as e:
            logging.error(f"[SYMBOL_TRACKER_NOTIFY] Error: {e}")

    return payload


def symbols_tracker_job():
    """
    Job chạy mỗi 10 phút:
    - Lấy danh sách symbol is_active = true trong bot_subscriptions
    - Mỗi symbol → run_symbol_tracker_once(send_notify=True)
    - ETHUSDT sẽ dùng run_eth_tracker_once (giữ nguyên ethdata, eth_cycles,...)
    """
    try:
        resp = (
            supabase_admin.table("bot_subscriptions")
            .select("symbol")
            .eq("is_active", True)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logging.error(f"[SYMBOL_TRACKER_JOB] Error fetch subscriptions: {e}")
        return

    for row in rows:
        symbol = (row.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            payload = run_symbol_tracker_once(symbol, send_notify=True)
            logging.info(
                f"[SYMBOL_TRACKER_JOB] {symbol}: action={payload['action']} price={payload['price']}"
            )
        except Exception as e:
            logging.error(f"[SYMBOL_TRACKER_JOB] {symbol}: error {e}")




        
def init_inline_rsi_dual(app_: FastAPI, scheduler: Optional[BackgroundScheduler] = None):
    app_.include_router(_rsi_router, prefix="/bots", tags=["bots"])
    if scheduler is not None:
        try:
            scheduler.add_job(
                symbols_tracker_job,
                "interval",
                minutes=10,
                id="symbols_tracker_job",
                replace_existing=True,
                next_run_time=datetime.utcnow(),
            )
        except Exception:
            scheduler.add_job(
                symbols_tracker_job,
                "interval",
                minutes=10,
                id="symbols_tracker_job",
                replace_existing=True,
            )
    else:
        import threading

        def _loop():
            while True:
                try:
                    _rsi_check_once()
                except Exception:
                    pass
                time.sleep(RSI_CHECK_MINUTES * 60)

        threading.Thread(target=_loop, daemon=True).start()


# ------------------------------------------------------------------
# 5) LESSON/SHARE ENDPOINTS
# ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def homepage(
    request: Request,
    userid: str | None = Query(None),
    groupid: str | None = Query(None),
    lessonid: str | None = Query(None),
    column: str | None = Query(None),
    print: str | None = Query(None),
    sort: str | None = Query(None),
):
    # Yêu cầu có lessonid để sinh lesson_id
    if not lessonid:
        return templates.TemplateResponse("landing.html", {"request": request})

    # 1) Tạo lesson_id từ lessonid + SALT bằng uuid5 (namespace DNS)
    lesson_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, lessonid + SALT))

    # 2) Decode base64 cho column/print -> list[int]
    hide_columns = _decode_b64_csv_to_ints(column)
    hide_columns_print = _decode_b64_csv_to_ints(print)

    # 3) Render như /{short_id} nhưng truy vấn theo id
    return await _process_lesson_by_id(request, lesson_id, hide_columns, hide_columns_print)


async def _process_lesson_by_id(
    request: Request, lesson_id: str, hide_columns: list[int], hide_columns_print: list[int]
):
    try:
        # Lấy lesson theo id
        lesson_resp = (
            supabase.table("lessons").select("id, name").eq("id", lesson_id).single().execute()
        )

        if not lesson_resp.data:
            raise ValueError(f"Lesson with id={lesson_id} not found")

        db_lesson_id = lesson_resp.data["id"]
        lesson_name = lesson_resp.data.get("name", f"Lesson {lesson_id}")

        # Lấy words theo lesson_id
        response = (
            supabase.table("words")
            .select("*")
            .eq("lesson_id", db_lesson_id)
            .order("latest_update", desc=False)
            .execute()
        )

        words_list = [
            {
                "word": row.get("word"),
                "type": row.get("type"),
                "pronunciation": row.get("pronunciation"),
                "meaning": row.get("meaning"),
                "translate": row.get("translate"),
                "example": row.get("example"),
                "word_voice": row.get("word_voice"),
                "eg_voice": row.get("eg_voice"),
                "trans_voice": row.get("trans_voice"),
                "df_voice": row.get("df_voice"),
            }
            for row in (response.data or [])
        ]

    except Exception as e:
        logging.error(f"[ERROR] Fetching data by lesson_id: {str(e)}")
        return templates.TemplateResponse("error.html", {"request": request, "error": str(e)})

    return templates.TemplateResponse(
        "share.html",
        {
            "request": request,
            "words": words_list,
            "lesson_id": lesson_id,  # hiển thị lesson_id đã sinh
            "lesson_name": lesson_name,
            "hide_columns": hide_columns,
            "hide_columns_print": hide_columns_print,
        },
    )


@app.get("/firebase", response_class=HTMLResponse)
async def firebase(
    request: Request,
    userid: str | None = Query(None),
    groupid: str | None = Query(None),
    lessonid: str | None = Query(None),
    column: str | None = Query(None, description="Base64 URL-safe chuỗi CSV, ví dụ: 'MSwyLDQ=' ~ '1,2,4'"),
    print: str | None = Query(None, description="Base64 URL-safe chuỗi CSV, ví dụ: 'NCw1' ~ '4,5'"),
    sort: str | None = Query(None),
):
    # Yêu cầu có lessonid để sinh lesson_id
    if not lessonid:
        return templates.TemplateResponse(
            "error.html", {"request": request, "error": "Missing required query param: lessonid"}
        )

    # 1) Tạo lesson_id từ lessonid + SALT bằng uuid5 (namespace DNS)
    lesson_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, lessonid + SALT))

    # 2) Decode base64 cho column/print -> list[int]
    hide_columns = _decode_b64_csv_to_ints(column)
    hide_columns_print = _decode_b64_csv_to_ints(print)

    # 3) Render như /{short_id} nhưng truy vấn theo id
    return await _process_lesson_by_id(request, lesson_id, hide_columns, hide_columns_print)


@app.get("/share", response_class=HTMLResponse)
async def share_lesson(
    request: Request,
    id: str = Query(..., description="Lesson short_id"),
    c: str = Query("", description="Ẩn nội dung cột khi hiển thị, vd: 1,2,4"),
    p: str = Query("", description="Ẩn nội dung cột khi in, vd: 4,5"),
):
    return await process_lesson(request, id, c, p)


async def process_lesson(request: Request, short_id: str, c: str, p: str):
    try:
        # Lấy lesson theo short_id
        lesson_resp = (
            supabase.table("lessons")
            .select("id, name")
            .eq("short_id", short_id.replace("!", ""))
            .single()
            .execute()
        )

        if not lesson_resp.data:
            raise ValueError(f"Lesson with short_id={short_id} not found")

        lesson_id = lesson_resp.data["id"]
        lesson_name = lesson_resp.data.get("name", f"Lesson {short_id}")

        # Lấy words theo lesson_id
        response = (
            supabase.table("words")
            .select("*")
            .eq("lesson_id", lesson_id)
            .order("latest_update", desc=False)
            .execute()
        )

        words_list = [
            {
                "word": row.get("word"),
                "type": row.get("type"),
                "pronunciation": row.get("pronunciation"),
                "meaning": row.get("meaning"),
                "translate": row.get("translate"),
                "example": row.get("example"),
                "word_voice": row.get("word_voice"),
                "eg_voice": row.get("eg_voice"),
                "trans_voice": row.get("trans_voice"),
                "df_voice": row.get("df_voice"),
            }
            for row in (response.data or [])
        ]

        # Shuffle words_list if short_id contains '!'
        if "!" in short_id:
            import random

            random.shuffle(words_list)

        # Parse params c, p
        hide_columns = [int(x) for x in c.split(",") if x.isdigit()]
        hide_columns_print = [int(x) for x in p.split(",") if x.isdigit()]

    except Exception as e:
        logging.error(f"[ERROR] Fetching data: {str(e)}")
        return templates.TemplateResponse("error.html", {"request": request, "error": str(e)})

    return templates.TemplateResponse(
        "share.html",
        {
            "request": request,
            "words": words_list,
            "lesson_id": short_id,
            "lesson_name": lesson_name,
            "hide_columns": hide_columns,
            "hide_columns_print": hide_columns_print,
        },
    )

# ------------------------------------------------------------------
# 6) KEYS MANAGEMENT
# ------------------------------------------------------------------
@app.get("/keys", response_class=HTMLResponse)
async def keys_page(request: Request):
    try:
        response = supabase.table("ttskeys").select("*").execute()
        keys = response.data or []
        for key in keys:
            if key.get("api_key") and len(key["api_key"]) > 4:
                key["api_key"] = key["api_key"][:-4] + "****"
    except Exception as e:
        logging.error(f"[ERROR] Fetching keys: {str(e)}")
        keys = []
    return templates.TemplateResponse("key.html", {"request": request, "keys": keys})


@app.post("/keys")
async def add_key(
    request: Request,
    api_key: str = Form(...),
    base_url: str = Form(...),
    provider: str = Form(...),
):
    try:
        data = {
            "api_key": api_key,
            "base_url": base_url,
            "provider": provider,
            "created_at": "now()",
            "is_live": True,
            "balance": 0.0,
            "description": "",
        }
        response = supabase.table("ttskeys").insert(data).execute()
        return templates.TemplateResponse(
            "key.html",
            {"request": request, "success": "Key added successfully", "keys": response.data},
        )
    except Exception as e:
        logging.error(f"[ERROR] Adding key: {str(e)}")
        return templates.TemplateResponse("key.html", {"request": request, "error": str(e)})


@app.post("/delete_key/{id}")
async def delete_key(request: Request, id: str):
    try:
        supabase.table("ttskeys").delete().eq("id", id).execute()
        return templates.TemplateResponse(
            "key.html", {"request": request, "success": "Key deleted successfully"}
        )
    except Exception as e:
        logging.error(f"[ERROR] Deleting key: {str(e)}")
        return templates.TemplateResponse("key.html", {"request": request, "error": str(e)})


# ------------------------------------------------------------------
# 7) USER ADMIN (RPC)
# ------------------------------------------------------------------
@app.get("/users", response_model=UsersListResponse)
async def list_users_rpc(
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=1000),
    search: str | None = Query(None, description="Lọc theo email (ILIKE)"),
    authorized: bool = Depends(verify_api_key),
):
    try:
        payload = {"p_page": page, "p_per_page": per_page, "p_search": search}
        resp = supabase_admin.rpc("admin_list_users", payload).execute()
        data = getattr(resp, "data", None)
        if not isinstance(data, dict):
            import json as _json

            data = _json.loads(data) if isinstance(data, str) else {}
        return UsersListResponse(**data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"admin_list_users failed: {e}")


@app.get("/user", response_model=UserStatsResponse)
async def get_user_stats_rpc(
    userid: str = Query(..., description="Supabase Auth user id (UUID)"),
    authorized: bool = Depends(verify_api_key),
):
    try:
        resp = supabase_admin.rpc("admin_get_user_stats", {"target_user_id": userid}).execute()
        data = getattr(resp, "data", None)
        if not isinstance(data, dict):
            import json as _json

            data = _json.loads(data) if isinstance(data, str) else {}
        return UserStatsResponse(**data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"admin_get_user_stats failed: {e}")


@app.delete("/removeData", response_model=ClearResult)
async def remove_user_data(
    userid: str = Query(..., description="Supabase Auth user id (UUID)"),
    authorized: bool = Depends(verify_api_key),
):
    """
    Xoá toàn bộ dữ liệu user bằng RPC admin_clear_user_data(target_user_id uuid)
    """
    if not UUID_RE.match(userid):
        raise HTTPException(status_code=422, detail="Invalid UUID format for userid")

    # 1) RPC xoá dữ liệu ứng dụng (security definer yêu cầu service_role)
    try:
        rpc_resp = supabase_admin.rpc("admin_clear_user_data", {"target_user_id": userid}).execute()
        if hasattr(rpc_resp, "model_dump"):
            raw = rpc_resp.model_dump()
            rpc_data = raw.get("data", raw)
        else:
            rpc_data = getattr(rpc_resp, "data", None) or {}
    except Exception as e:
        # Không xoá Auth nếu RPC thất bại để tránh mồ côi dữ liệu
        raise HTTPException(status_code=500, detail=f"RPC admin_clear_user_data failed: {e}")

    return ClearResult(
        status="ok",
        userid=userid,
        rpc_result=rpc_data if isinstance(rpc_data, dict) else {"data": rpc_data},
        auth_deleted=False,
        note="RPC only.",
    )


@app.delete("/remove", response_model=ClearResult)
async def remove_user(
    userid: str = Query(..., description="Supabase Auth user id (UUID)"),
    authorized: bool = Depends(verify_api_key),
):
    """
    Xoá toàn bộ dữ liệu user bằng RPC admin_clear_user_data(target_user_id uuid)
    rồi xoá user khỏi Supabase Auth (admin). Thứ tự: RPC -> Auth.
    """
    if not UUID_RE.match(userid):
        raise HTTPException(status_code=422, detail="Invalid UUID format for userid")

    # 1) RPC xoá dữ liệu ứng dụng (security definer yêu cầu service_role)
    try:
        rpc_resp = supabase_admin.rpc("admin_clear_user_data", {"target_user_id": userid}).execute()
        if hasattr(rpc_resp, "model_dump"):
            raw = rpc_resp.model_dump()
            rpc_data = raw.get("data", raw)
        else:
            rpc_data = getattr(rpc_resp, "data", None) or {}
    except Exception as e:
        # Không xoá Auth nếu RPC thất bại để tránh mồ côi dữ liệu
        raise HTTPException(status_code=500, detail=f"RPC admin_clear_user_data failed: {e}")

    # 2) Xoá user khỏi Supabase Auth
    try:
        supabase_admin.auth.admin.delete_user(userid)
        auth_deleted = True
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auth delete failed after RPC succeeded: {e}")

    return ClearResult(
        status="ok",
        userid=userid,
        rpc_result=rpc_data if isinstance(rpc_data, dict) else {"data": rpc_data},
        auth_deleted=auth_deleted,
        note="RPC done first, then Auth deleted.",
    )



# ------------------------------------------------------------------
# 8) STATIC/INFO PAGES
# ------------------------------------------------------------------
@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse("fasteng-privacy-policy.html", {"request": request})


@app.get("/privacypolicy", response_class=HTMLResponse)
async def privacypolicy_page(request: Request):
    return templates.TemplateResponse("fasteng-privacy-policy.html", {"request": request})


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse("fasteng-terms.html", {"request": request})


@app.get("/app-ads.txt", include_in_schema=False)
def get_app_ads():
    if not APP_ADS_PATH.exists():
        raise HTTPException(status_code=404, detail="app-ads.txt not found")
    headers = {"Cache-Control": "public, max-age=86400"}  # cache 1 ngày
    return FileResponse(APP_ADS_PATH, media_type="text/plain; charset=utf-8", headers=headers)

@app.get("/favicon.ico", include_in_schema=False)
def get_favicon_ico():
    if not APP_FAVICON_PATH.exists():
        raise HTTPException(status_code=404, detail="favicon.ico not found")
    headers = {"Cache-Control": "public, max-age=86400"}  # cache 1 ngày
    return FileResponse(APP_FAVICON_PATH, media_type="image/x-icon")
        
    
# ------------------------------------------------------------------
# 9) GOOGLE EID SCRAPER
# ------------------------------------------------------------------
@app.get("/geteid")
def get_eid(version: str = "v9.2.0"):
    try:
        headers = {
            "Sec-Fetch-Site": "none",
            "Connection": "keep-alive",
            "Sec-Fetch-Mode": "navigate",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "document",
        }
        url = f"https://googleads.g.doubleclick.net/mads/static/sdk/native/sdk-core-v40.html?sdk=afma-sdk-i-{version}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        eid1_match = re.search(r'var sdkLoaderEID = "([^"]+)"', html_content)
        eid2_match = re.search(r',e.includes\("([^"]+)"\)', html_content)

        sdkLoaderEID = eid1_match.group(1) if eid1_match else None
        sdkLoaderEID2 = eid2_match.group(1) if eid2_match else None

        return {"sdkLoaderEID": sdkLoaderEID, "sdkLoaderEID2": sdkLoaderEID2, "version": version}
    except Exception as e:
        logging.error(f"Error fetching EID values: {str(e)}")
        return {"sdkLoaderEID": "318502621", "sdkLoaderEID2": "318500618", "version": "v9.2.0"}


@_rsi_router.post("/subscribe")
async def subscribe_symbol(symbol: str = Form(...)):
    """
    SUBSCRIBE 1 symbol vào danh sách theo dõi.
    """
    symbol = symbol.upper()
    try:
        # upsert theo symbol
        supabase_admin.table("bot_subscriptions") \
            .upsert(
                {"symbol": symbol, "is_active": True},
                on_conflict="symbol",
            ) \
            .execute()
    except Exception as e:
        logging.error(f"[SUBSCRIBE] Error subscribe {symbol}: {e}")

    return RedirectResponse(url=f"/bots/{symbol}", status_code=303)


@_rsi_router.post("/unsubscribe")
async def unsubscribe_symbol(symbol: str = Form(...)):
    """
    UNSUBSCRIBE 1 symbol khỏi danh sách theo dõi.
    """
    symbol = symbol.upper()
    try:
        supabase_admin.table("bot_subscriptions") \
            .update({"is_active": False}) \
            .eq("symbol", symbol) \
            .execute()
    except Exception as e:
        logging.error(f"[UNSUBSCRIBE] Error unsubscribe {symbol}: {e}")

    return RedirectResponse(url=f"/bots/{symbol}", status_code=303)


@_rsi_router.get("/big", response_class=HTMLResponse)
async def big_trades_dashboard(request: Request):
    """
    Dashboard Big Orders:
    - Tính tổng giá trị BUY / SELL trong 24h qua cho BTCUSDT & ETHUSDT
    - Tính %BUY / %SELL
    - Đếm số lệnh BUY / SELL
    - Tìm lệnh BUY / SELL có notional lớn nhất cho từng symbol
    - Tính tổng giá trị BUY / SELL theo từng vùng giá:
        + ETHUSDT: mỗi vùng 50$
        + BTCUSDT: mỗi vùng 500$
      và đếm số lệnh trong từng vùng.
    """
    symbols = ["BTCUSDT", "ETHUSDT"]

    if supabase_admin is None:
        logging.warning("[BIG_TRADES] supabase_admin is None, render empty dashboard")
        context = {
            "request": request,
            "has_data": False,
            "summary": {},
            "buckets": {},
            "from_time": None,
            "to_time": None,
        }
        return templates.TemplateResponse("big_dashboard.html", context)

    now_utc = datetime.utcnow()
    since_utc = now_utc - timedelta(hours=24)

    # Lấy dữ liệu 24h gần nhất từ bảng big_trades
    try:
        resp = (
            supabase_admin.table("big_trades")
            .select("symbol, trade_time, price, qty, notional_usdt, side")
            .gte("trade_time", since_utc.isoformat())
            .in_("symbol", symbols)
            .execute()
        )
        rows = resp.data or []
    except Exception as e:
        logging.error(f"[BIG_TRADES] Error fetch big_trades: {e}")
        rows = []

    if not rows:
        context = {
            "request": request,
            "has_data": False,
            "summary": {},
            "buckets": {},
            "from_time": since_utc,
            "to_time": now_utc,
        }
        return templates.TemplateResponse("big_dashboard.html", context)

    # -----------------------------------------
    # 1) Tổng BUY/SELL & đếm số lệnh
    # -----------------------------------------
    summary_notional: Dict[str, Dict[str, float]] = {
        "BTCUSDT": {"BUY": 0.0, "SELL": 0.0},
        "ETHUSDT": {"BUY": 0.0, "SELL": 0.0},
    }
    summary_counts: Dict[str, Dict[str, int]] = {
        "BTCUSDT": {"BUY": 0, "SELL": 0},
        "ETHUSDT": {"BUY": 0, "SELL": 0},
    }

    # -----------------------------------------
    # 2) Largest BUY/SELL
    # -----------------------------------------
    # largest_trades[symbol][side] = {notional, price, qty, time}
    largest_trades: Dict[str, Dict[str, Dict[str, Any]]] = {
        "BTCUSDT": {
            "BUY": {"notional": 0.0, "price": None, "qty": None, "time": None},
            "SELL": {"notional": 0.0, "price": None, "qty": None, "time": None},
        },
        "ETHUSDT": {
            "BUY": {"notional": 0.0, "price": None, "qty": None, "time": None},
            "SELL": {"notional": 0.0, "price": None, "qty": None, "time": None},
        },
    }

    # -----------------------------------------
    # 3) Buckets theo vùng giá
    # -----------------------------------------
    # buckets[symbol][bucket_index] = {
    #   low, high, BUY, SELL, buy_count, sell_count
    # }
    buckets: Dict[str, Dict[int, Dict[str, Any]]] = {
        "BTCUSDT": {},
        "ETHUSDT": {},
    }

    for row in rows:
        symbol = (row.get("symbol") or "").upper()
        if symbol not in summary_notional:
            continue

        side = (row.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            continue

        try:
            price = float(row.get("price") or 0)
            notional = float(row.get("notional_usdt") or 0)
            qty = float(row.get("qty") or 0)
            trade_time = row.get("trade_time")
        except Exception:
            continue

        # Tổng notional
        summary_notional[symbol][side] += notional
        # Đếm số lệnh
        summary_counts[symbol][side] += 1

        # Largest trade theo side
        cur_largest = largest_trades[symbol][side]
        if notional > cur_largest["notional"]:
            largest_trades[symbol][side] = {
                "notional": notional,
                "price": price,
                "qty": qty,
                "time": trade_time,
            }

        # Buckets theo vùng giá
        step = 1000.0 if symbol == "BTCUSDT" else 50.0
        bucket_index = int(price // step)
        low = bucket_index * step
        high = (bucket_index + 1) * step

        symbol_buckets = buckets[symbol]
        if bucket_index not in symbol_buckets:
            symbol_buckets[bucket_index] = {
                "low": low,
                "high": high,
                "BUY": 0.0,
                "SELL": 0.0,
                "buy_count": 0,
                "sell_count": 0,
            }

        # Cộng tiền
        symbol_buckets[bucket_index][side] += notional
        # Cộng số lệnh
        if side == "BUY":
            symbol_buckets[bucket_index]["buy_count"] += 1
        else:
            symbol_buckets[bucket_index]["sell_count"] += 1

    # -----------------------------------------
    # 4) Summary view + %BUY/%SELL + largest
    # -----------------------------------------
    summary_view: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        buy_val = summary_notional[sym]["BUY"]
        sell_val = summary_notional[sym]["SELL"]
        total = buy_val + sell_val

        if total > 0:
            pct_buy = buy_val / total * 100.0
            pct_sell = sell_val / total * 100.0
        else:
            pct_buy = pct_sell = 0.0

        summary_view[sym] = {
            "buy": buy_val,
            "sell": sell_val,
            "total": total,
            "pct_buy": pct_buy,
            "pct_sell": pct_sell,
            "buy_count": summary_counts[sym]["BUY"],
            "sell_count": summary_counts[sym]["SELL"],
            "largest_buy": largest_trades[sym]["BUY"],
            "largest_sell": largest_trades[sym]["SELL"],
        }

    # -----------------------------------------
    # 5) Buckets view
    # -----------------------------------------
    buckets_view: Dict[str, List[Dict[str, Any]]] = {}

    for sym in symbols:
        sym_buckets = buckets[sym]
        if not sym_buckets:
            buckets_view[sym] = []
            continue

        rows_list: List[Dict[str, Any]] = []
        for idx, info in sym_buckets.items():
            low = info["low"]
            high = info["high"]
            buy_val = info["BUY"]
            sell_val = info["SELL"]
            total = buy_val + sell_val
            buy_count = info["buy_count"]
            sell_count = info["sell_count"]

            if buy_val > sell_val:
                dominance = "BUY"
            elif sell_val > buy_val:
                dominance = "SELL"
            else:
                dominance = "BALANCED"

            rows_list.append(
                {
                    "range_str": f"{low:.0f} – {high:.0f}",
                    "buy": buy_val,
                    "sell": sell_val,
                    "total": total,
                    "dominance": dominance,
                    "buy_count": buy_count,
                    "sell_count": sell_count,
                }
            )

        rows_list.sort(key=lambda r: float(r["range_str"].split("–")[0]))
        buckets_view[sym] = rows_list

    context = {
        "request": request,
        "has_data": True,
        "summary": summary_view,
        "buckets": buckets_view,
        "from_time": since_utc,
        "to_time": now_utc,
    }
    return templates.TemplateResponse("big_dashboard.html", context)

@app.get("/code", response_class=HTMLResponse)
async def code_get(request: Request):
    """
    Render form encode/decode.
    """
    return templates.TemplateResponse(
        "code.html",
        {
            "request": request,
            "mode": "encode",
            "input_data": "",
            "password": "",
            "salt": "",
            "result": "",
            "error": "",
        },
    )

@app.post("/code", response_class=HTMLResponse)
async def code_post(
    request: Request,
    mode: str = Form(...),      # "encode" hoặc "decode"
    input_data: str = Form(...),
    password: str = Form(...),
    salt: str = Form(...),
):
    error = ""
    result = ""

    if not input_data or not password or not salt:
        error = "Vui lòng nhập đầy đủ: Data, Password, Salt."
    else:
        try:
            fernet = get_fernet(password, salt)

            if mode == "encode":
                token = fernet.encrypt(input_data.encode("utf-8"))
                result = token.decode("utf-8")
            elif mode == "decode":
                try:
                    decoded = fernet.decrypt(input_data.encode("utf-8"))
                    result = decoded.decode("utf-8")
                except InvalidToken:
                    error = "Giải mã thất bại: sai password/salt hoặc chuỗi không hợp lệ."
            else:
                error = "Mode không hợp lệ."
        except Exception as e:
            error = f"Lỗi: {e}"

    return templates.TemplateResponse(
        "code.html",
        {
            "request": request,
            "mode": mode,
            "input_data": input_data,
            "password": password,
            "salt": salt,
            "result": result,
            "error": error,
        },
    )


@_rsi_router.get("/{symbol}", response_class=HTMLResponse)
async def symbol_dashboard(request: Request, symbol: str):
    """
    Dashboard theo dõi bất kỳ symbol nào (BTCUSDT, ETHUSDT, BNBUSDT...):
    - Giá, RSI, %change 24h
    - Buy/Sell zone (dynamic)
    - BUY/SELL signals (client-side: RSI + BB + Stoch + Williams %R + EMA trend)
    - tracker_action/server (BUY/SELL/HOLD) dùng cùng logic với bot (ethtracker)
    - SUBSCRIBE/UNSUBSCRIBE symbol này.
    """
    symbol = symbol.upper()

    # 1) Lấy klines từ Binance
    try:
        klines = _rsi_fetch_klines(symbol, TRACKER_INTERVAL, limit=200)
    except Exception as e:
        logging.error(f"[SYMBOL DASH] Error fetching klines for {symbol}: {e}")
        klines = []

    labels: List[str] = []
    closes: List[float] = []
    highs: List[float] = []
    lows: List[float] = []

    for k in klines:
        try:
            open_time_ms = int(k[0])
            dt = datetime.utcfromtimestamp(open_time_ms / 1000.0)
            labels.append(dt.strftime("%Y-%m-%d %H:%M"))

            o = float(k[1])
            h = float(k[2])
            l = float(k[3])
            c = float(k[4])

            highs.append(h)
            lows.append(l)
            closes.append(c)
        except Exception as e:
            logging.warning(f"[SYMBOL DASH] Bad kline row for {symbol}: {e}")
            continue

    if not closes:
        context = {
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
            "is_subscribed": False,
            "tracker_action": "HOLD",
            "tracker_reason": "No data",
        }
        return templates.TemplateResponse("symbol_dashboard.html", context)

    # 2) Indicator series
    rsi_values = _compute_rsi_series(closes, RSI_PERIOD)
    macd_line, macd_signal, macd_hist_values = _compute_macd_series(
        closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL
    )
    ema_fast = _compute_ema_series(closes, 12)
    ema_slow = _compute_ema_series(closes, 26)
    sma_50 = _sma_series(closes, 50)

    bb_middle, bb_upper, bb_lower = _bollinger_bands(closes, period=20, k=2.0)
    stoch_k = _stochastic_oscillator(highs, lows, closes, period=14)
    williams_r = _williams_r(highs, lows, closes, period=14)

    # 3) Dynamic zones
    buy_low = buy_high = sell_low = sell_high = recent_low = recent_high = None
    try:
        zones = _compute_eth_zones_from_range(symbol, TRACKER_INTERVAL, lookback=60)
        sell_low, sell_high, buy_low, buy_high, recent_low, recent_high = zones
    except Exception as e:
        logging.error(f"[SYMBOL DASH] Error computing zones for {symbol}: {e}")

    # 4) Align length
    n = len(closes)
    min_len = min(
        n,
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
    highs = highs[-min_len:]
    lows = lows[-min_len:]

    # 5) rows_json cho JS vẽ chart
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

    # 6) Price / RSI / %change 24h
    last_price = closes[-1]
    last_rsi = rsi_values[-1] if rsi_values else None

    change_24h = None
    try:
        if len(closes) >= 7:
            ref = closes[-7]
            if ref != 0:
                change_24h = (last_price - ref) / ref * 100.0
    except Exception:
        change_24h = None

    # 7) Server-side tracker action (logic giống bot)
    tracker_action = "HOLD"
    tracker_reason = ""
    try:
        payload = run_symbol_tracker_once(symbol, send_notify=False)
        tracker_action = payload.get("action", "HOLD")
        tracker_reason = payload.get("reason", "")
    except Exception as e:
        logging.error(f"[SYMBOL DASH] Error run_symbol_tracker_once for {symbol}: {e}")

    # 8) Check subscription
    is_subscribed = False
    try:
        resp = (
            supabase_admin.table("bot_subscriptions")
            .select("is_active")
            .eq("symbol", symbol)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if rows and rows[0].get("is_active"):
            is_subscribed = True
    except Exception as e:
        logging.error(f"[SYMBOL DASH] Error check subscription for {symbol}: {e}")

    context = {
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
        "is_subscribed": is_subscribed,
        "tracker_action": tracker_action,
        "tracker_reason": tracker_reason,
    }

    return templates.TemplateResponse("symbol_dashboard.html", context)

@app.get("/{short_id}", response_class=HTMLResponse)
async def share_lesson_by_short_id(
    request: Request,
    short_id: str,
    c: str = Query("", description="Ẩn nội dung cột khi hiển thị, vd: 1,2,4"),
    p: str = Query("", description="Ẩn nội dung cột khi in, vd: 4,5"),
):
    return await process_lesson(request, short_id, c, p)
# ------------------------------------------------------------------
# 10) SCHEDULER INIT
# ------------------------------------------------------------------
scheduler = BackgroundScheduler()
init_inline_rsi_dual(app, scheduler)
scheduler.start()


# ------------------------------------------------------------------
# 11) ENTRY POINT
# ------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        reload=True,
        workers=1,
    )
