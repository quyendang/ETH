
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
from datetime import datetime
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
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from supabase import create_client, Client

# ------------------------------------------------------------------
# 1) GLOBAL APP/ENV CONFIG
# ------------------------------------------------------------------
app = FastAPI()
templates = Jinja2Templates(directory="templates")

BASE_DIR = Path(__file__).resolve().parent
APP_ADS_PATH = BASE_DIR / "app-ads.txt"

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
    Quyết định BUY/SELL/HOLD cho ETH với:
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

    # ETH: MACD hist đang yếu đi? (đỉnh tròn)
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

    # Info thêm về MACD ETH
    if abs(macd_hist) < 0.5:
        reasons.append("MACD hist ~0 → ETH momentum weak / sideway.")
    elif macd_hist > 0:
        reasons.append("MACD hist > 0 → ETH bullish momentum.")
    else:
        reasons.append("MACD hist < 0 → ETH bearish momentum.")

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

    
# ===== ETH TRACKER CORE =====

def run_eth_tracker_once(send_notify: bool = False):
    symbol = ETH_TRACKER_SYMBOL
    interval = ETH_TRACKER_INTERVAL

    # 1) ETH Price + RSI H4
    price, rsi_h4 = _rsi_latest(symbol, interval, RSI_PERIOD)

    # 2) ETH MACD + prev hist
    macd_line, macd_signal, macd_hist, prev_macd_hist = _macd_latest_with_prev(
        symbol,
        interval,
    )

    # 3) BTC Price + RSI H4
    btc_price, btc_rsi_h4 = _rsi_latest("BTCUSDT", interval, RSI_PERIOD)

    # 4) BTC MACD + prev hist
    btc_macd_line, btc_macd_signal, btc_macd_hist, btc_prev_macd_hist = _macd_latest_with_prev(
        "BTCUSDT",
        interval,
    )

    # 5) Dynamic zones ETH
    zones = _compute_eth_zones_from_range(
        symbol,
        interval,
        lookback=60,
    )
    sell_low, sell_high, buy_low, buy_high, recent_low, recent_high = zones

    # 6) Quyết định action có filter BTC
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


    # ==== ETH CYCLE TRACKING ====
    try:
        if action == "SELL":
            # Mở 1 cycle mới
            cycle_index = _get_next_cycle_index()
            supabase_admin.table("eth_cycles").insert(
                {
                    "cycle_index": cycle_index,
                    "sell_price": price,
                    "amount_eth": ETH_CYCLE_SIZE,
                }
            ).execute()

        elif action == "BUY":
            # Đóng cycle gần nhất (nếu có)
            open_cycle = _get_open_cycle()
            if open_cycle:
                sell_price = float(open_cycle["sell_price"])
                buy_price = price
                amount = float(open_cycle["amount_eth"])

                delta_usdt = (sell_price - buy_price) * amount
                # Nếu sell cao hơn buy → delta_usdt > 0 → có lợi nhuận
                delta_eth = delta_usdt / buy_price if buy_price != 0 else 0.0

                supabase_admin.table("eth_cycles").update(
                    {
                        "buy_price": buy_price,
                        "delta_usdt": delta_usdt,
                        "delta_eth": delta_eth,
                    }
                ).eq("id", open_cycle["id"]).execute()
    except Exception as e:
        logging.error(f"[ETHCYCLES] Error updating cycles: {e}")


    now_utc = datetime.utcnow().isoformat() + "Z"

    payload = {
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
        },
    }

    # 7) Lưu ethdata như cũ
    try:
        supabase_admin.table("ethdata").insert(
            {
                "symbol": symbol,
                "timeframe": interval,
                "price": price,
                "rsi_h4": rsi_h4,
                "macd": macd_line,
                "macd_signal": macd_signal,
                "macd_hist": macd_hist,
                "action": action,
                "reason": reason,
            }
        ).execute()
    except Exception as e:
        logging.error(f"[ETHTRACKER] Error inserting into Supabase: {e}")

    # 8) Gửi Pushover nếu cần
    if send_notify and action != "HOLD":
        try:
            title = f"ETH Tracker: {action}"
            msg_lines = [
                f"Action: {action}",
                f"Reason: {reason}",
                f"Price: {price}",
                f"RSI H4: {rsi_h4}",
                f"MACD: {macd_line:.4f} | Signal: {macd_signal:.4f} | Hist: {macd_hist:.4f}",
                f"BTC RSI H4: {btc_rsi_h4:.1f}, BTC hist: {btc_macd_hist:.4f}",
                f"Time (UTC): {now_utc}",
            ]
            _pushover_notify(title, "\n".join(msg_lines))
        except Exception as e:
            logging.error(f"[ETHTRACKER] Error sending Pushover: {e}")

    return payload



# ===== API ENDPOINT =====

@_rsi_router.get("/eth", response_class=HTMLResponse)
async def eth_dashboard(request: Request):
    """
    ETH dashboard:
    - Chart price/RSI/MACD + BUY/SELL + zones
    - Bảng các vòng xoay eth_cycles
    """
    # ====== 1) Lấy dữ liệu ETHDATA để vẽ chart ======
    try:
        resp = supabase_admin.table("ethdata") \
            .select("*") \
            .order("created_at", desc=False) \
            .limit(500) \
            .execute()
        rows = resp.data or []
    except Exception as e:
        logging.error(f"[ETHDATA] Error fetching from Supabase: {e}")
        rows = []

    labels = []
    prices = []
    rsi_values = []
    macd_hist_values = []
    buy_points = []
    sell_points = []

    for r in rows:
        ts = r.get("created_at")
        labels.append(ts)

        price = float(r.get("price", 0))
        rsi = float(r.get("rsi_h4", 0))
        macd_hist = float(r.get("macd_hist", 0))
        action = r.get("action", "HOLD")

        prices.append(price)
        rsi_values.append(rsi)
        macd_hist_values.append(macd_hist)

        if action == "BUY":
            buy_points.append(price)
            sell_points.append(None)
        elif action == "SELL":
            buy_points.append(None)
            sell_points.append(price)
        else:
            buy_points.append(None)
            sell_points.append(None)

    # Dynamic zones từ logic hiện tại
    buy_low = buy_high = sell_low = sell_high = recent_low = recent_high = None
    try:
        zones = _compute_eth_zones_from_range(
            ETH_TRACKER_SYMBOL,
            ETH_TRACKER_INTERVAL,
            lookback=60,
        )
        sell_low, sell_high, buy_low, buy_high, recent_low, recent_high = zones
    except Exception as e:
        logging.error(f"[ETHDATA] Error computing zones: {e}")

    # ====== 2) Lấy dữ liệu ETH CYCLES ======
    try:
        cycles_resp = supabase_admin.table("eth_cycles") \
            .select("*") \
            .order("cycle_index", desc=False) \
            .execute()
        cycles = cycles_resp.data or []
    except Exception as e:
        logging.error(f"[ETHCYCLES] Error fetching cycles: {e}")
        cycles = []

    # Tổng ETH free tích luỹ
    total_delta_eth = 0.0
    for c in cycles:
        d = c.get("delta_eth")
        if d is not None:
            total_delta_eth += float(d)

    final_eth = ETH_BASE_BALANCE + total_delta_eth

    context = {
        "request": request,
        # Chart data
        "labels": labels,
        "prices": prices,
        "rsi_values": rsi_values,
        "macd_hist_values": macd_hist_values,
        "buy_points": buy_points,
        "sell_points": sell_points,
        "buy_low": buy_low,
        "buy_high": buy_high,
        "sell_low": sell_low,
        "sell_high": sell_high,
        "recent_low": recent_low,
        "recent_high": recent_high,
        # Cycles
        "cycles": cycles,
        "base_eth": ETH_BASE_BALANCE,
        "delta_eth_total": total_delta_eth,
        "final_eth": final_eth,
    }
    return templates.TemplateResponse("eth_dashboard.html", context)



@_rsi_router.get("/ethtracker")
def eth_tracker():
    """
    Endpoint HTTP để xem nhanh dữ liệu tracker hiện tại.
    Không gửi Pushover, chỉ trả JSON.
    """
    return run_eth_tracker_once(send_notify=False)

@_rsi_router.get("/ethdata", response_class=HTMLResponse)
async def bots_ethdata(request: Request):
    """
    Render chart ETH tracker từ dữ liệu bảng ethdata + vùng BUY/SELL dynamic.
    """
    try:
        # Lấy tối đa 500 record gần nhất, sắp xếp theo created_at tăng dần
        resp = supabase_admin.table("ethdata") \
            .select("*") \
            .order("created_at", desc=False) \
            .limit(500) \
            .execute()
        rows = resp.data or []
    except Exception as e:
        logging.error(f"[ETHDATA] Error fetching from Supabase: {e}")
        rows = []

    labels = []
    prices = []
    rsi_values = []
    macd_hist_values = []
    buy_points = []
    sell_points = []

    for r in rows:
        ts = r.get("created_at")
        labels.append(ts)

        price = float(r.get("price", 0))
        rsi = float(r.get("rsi_h4", 0))
        macd_hist = float(r.get("macd_hist", 0))
        action = r.get("action", "HOLD")

        prices.append(price)
        rsi_values.append(rsi)
        macd_hist_values.append(macd_hist)

        if action == "BUY":
            buy_points.append(price)
            sell_points.append(None)
        elif action == "SELL":
            buy_points.append(None)
            sell_points.append(price)
        else:
            buy_points.append(None)
            sell_points.append(None)

    # 🔥 TÍNH VÙNG GIÁ ĐỘNG ĐỂ VẼ ZONE
    buy_low = buy_high = sell_low = sell_high = recent_low = recent_high = None
    try:
        zones = _compute_eth_zones_from_range(
            ETH_TRACKER_SYMBOL,
            ETH_TRACKER_INTERVAL,
            lookback=60,  # 60 nến H4 ~ 10 ngày
        )
        sell_low, sell_high, buy_low, buy_high, recent_low, recent_high = zones
    except Exception as e:
        logging.error(f"[ETHDATA] Error computing zones: {e}")

    context = {
        "request": request,
        "labels": labels,
        "prices": prices,
        "rsi_values": rsi_values,
        "macd_hist_values": macd_hist_values,
        "buy_points": buy_points,
        "sell_points": sell_points,
        # Zones để vẽ nền:
        "buy_low": buy_low,
        "buy_high": buy_high,
        "sell_low": sell_low,
        "sell_high": sell_high,
        "recent_low": recent_low,
        "recent_high": recent_high,
    }
    return templates.TemplateResponse("chart.html", context)


@_rsi_router.get("/rsi-status", response_class=JSONResponse)
def rsi_status():
    data = {
        "symbols": RSI_SYMBOLS,
        "period": RSI_PERIOD,
        "timeframes": RSI_TIMEFRAMES,
        "last_run_utc": datetime.utcfromtimestamp(_rsi_last_run).strftime("%Y-%m-%d %H:%M:%S") if _rsi_last_run else None,
        "values": _rsi_last_values,
        "state": _rsi_last_state,
        "check_every_minutes": RSI_CHECK_MINUTES,
    }
    # Tạo JSON pretty print
    pretty = json.dumps(data, indent=4, ensure_ascii=False)
    return JSONResponse(content=json.loads(pretty))

def eth_tracker_job():
    """
    Job chạy mỗi 30 phút:
    - Gọi run_eth_tracker_once(send_notify=True)
    - Lưu DB + gửi pushover nếu action != HOLD
    """
    try:
        payload = run_eth_tracker_once(send_notify=True)
        logging.info(f"[ETHTRACKER] Job run, action={payload['action']}, price={payload['price']}")
    except Exception as e:
        logging.error(f"[ETHTRACKER] Job error: {e}")
        
def init_inline_rsi_dual(app_: FastAPI, scheduler: Optional[BackgroundScheduler] = None):
    app_.include_router(_rsi_router, prefix="/bots", tags=["bots"])
    if scheduler is not None:
        try:
            scheduler.add_job(
                _rsi_check_once,
                "interval",
                minutes=RSI_CHECK_MINUTES,
                id="rsi_check_dual",
                replace_existing=True,
                next_run_time=datetime.utcnow(),
            )
            scheduler.add_job(
                eth_tracker_job,
                "interval",
                minutes=10,
                id="eth_tracker_job",
                replace_existing=True,
            )
        except Exception:
            scheduler.add_job(
                _rsi_check_once,
                "interval",
                minutes=RSI_CHECK_MINUTES,
                id="rsi_check_dual",
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
