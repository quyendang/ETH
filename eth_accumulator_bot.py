#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║   ETH ACCUMULATOR SIGNAL BOT v1.0                                    ║
║   Balanced All-In Strategy — Pushover Notifications                  ║
║                                                                      ║
║   Deploy: Koyeb (cron 4H) or any scheduler                          ║
║   Data:   Binance public API (no key needed)                         ║
║   Alert:  Pushover API                                               ║
║                                                                      ║
║   Logic:                                                             ║
║   - Tính indicators từ Binance klines (4H + 1D)                     ║
║   - Phát tín hiệu SELL ETH→USDT hoặc BUY USDT→ETH                 ║
║   - Adaptive sell%: giảm sau thua, tăng sau thắng                   ║
║   - Gửi alert qua Pushover với chi tiết hành động                   ║
║                                                                      ║
║   ENV vars:                                                          ║
║     PUSHOVER_USER_KEY    — Pushover user key                         ║
║     PUSHOVER_API_TOKEN   — Pushover app token                        ║
║     STATE_FILE           — Path to state JSON (default: state.json)  ║
║     COIN_SYMBOL          — Trading pair (default: ETHUSDT)           ║
║     COIN_NAME            — Display name (default: ETH)               ║
║     INITIAL_AMOUNT       — Starting coin amount (default: 100)       ║
║     DRY_RUN              — "true" to skip notifications              ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import time
import logging
import hashlib
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Tuple
from pathlib import Path

import requests
import numpy as np
import pandas as pd

# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("accumulator-bot")


# ═══════════════════════════════════════════════════════════════
#  CONFIG — Balanced profile (backtest +16.95 ETH, MaxDD -5.1%)
# ═══════════════════════════════════════════════════════════════

@dataclass
class BotConfig:
    # Adaptive sell
    initial_sell_pct: float = 1.00
    min_sell_pct: float = 0.20
    reduce_on_loss: float = 0.30
    increase_on_win: float = 0.10

    # Buyback thresholds
    stop_rise_pct: float = 1.5
    target_drop_pct: float = 2.0
    timeout_bars: int = 36
    oversold_rsi: float = 25.0

    # Sell signal filters
    sell_rsi_min: float = 58.0
    sell_cooldown_bars: int = 18

    # Sell signal thresholds
    sell_sig_a_rsi: float = 58.0
    sell_sig_a_sell_score: int = 3
    sell_sig_b_rsi: float = 55.0
    sell_sig_c_sell_score: int = 4
    sell_sig_c_rsi: float = 50.0
    sell_sig_c_stoch: float = 60.0

    commission_pct: float = 0.075


CFG = BotConfig()


# ═══════════════════════════════════════════════════════════════
#  BINANCE DATA FETCHER
# ═══════════════════════════════════════════════════════════════

BINANCE_BASE = "https://api.binance.com"


def fetch_klines(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    """Fetch klines from Binance public API."""
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            log.warning(f"Binance API attempt {attempt+1} failed: {e}")
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "num_trades",
        "taker_buy_vol", "taker_buy_quote", "ignore",
    ])

    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[col] = df[col].astype(float)

    df["datetime"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.sort_values("datetime").reset_index(drop=True)

    return df


# ═══════════════════════════════════════════════════════════════
#  INDICATOR COMPUTATION
# ═══════════════════════════════════════════════════════════════

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_macd(close: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema12 = compute_ema(close, 12)
    ema26 = compute_ema(close, 26)
    macd_line = ema12 - ema26
    signal = compute_ema(macd_line, 9)
    hist = macd_line - signal
    return macd_line, signal, hist


def compute_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def compute_bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return upper, mid, lower, pct_b


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h_l = df["high"] - df["low"]
    h_pc = (df["high"] - df["close"].shift(1)).abs()
    l_pc = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period).mean()


def compute_adx(df: pd.DataFrame, period: int = 14):
    high = df["high"]; low = df["low"]; close = df["close"]
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    mask = plus_dm > minus_dm
    plus_dm = plus_dm.where(mask, 0)
    minus_dm = minus_dm.where(~mask, 0)

    atr = compute_atr(df, period)
    di_plus = 100 * plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr.replace(0, np.nan)
    di_minus = 100 * minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr.replace(0, np.nan)
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = dx.ewm(alpha=1/period, min_periods=period).mean()
    return adx, di_plus, di_minus


def compute_sell_score(df: pd.DataFrame) -> pd.Series:
    """Simplified sell_score based on overbought indicators."""
    score = pd.Series(0, index=df.index)
    score += (df["rsi_14"] > 65).astype(int)
    score += (df["stoch_k"] > 75).astype(int)
    score += (df["bb_pct"] > 0.85).astype(int)
    score += (df["macd_hist"] < df["macd_hist"].shift(1)).astype(int)
    score += (df["close"] > df["ema_34"]).astype(int) & (df["close"] > df["ema_50"]).astype(int)
    score += (df["adx_14"] > 20).astype(int) & (df["di_minus"] > df["di_plus"]).astype(int)
    return score


def enrich_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all required indicators on 4H data."""
    c = df["close"]

    df["rsi_14"] = compute_rsi(c, 14)
    df["ema_34"] = compute_ema(c, 34)
    df["ema_50"] = compute_ema(c, 50)
    df["ema_200"] = compute_ema(c, 200)
    df["macd_line"], df["macd_signal"], df["macd_hist"] = compute_macd(c)
    df["macd_rising"] = (df["macd_hist"] > df["macd_hist"].shift(1)).astype(int)
    df["stoch_k"], df["stoch_d"] = compute_stochastic(df)
    df["bb_upper"], df["bb_mid"], df["bb_lower"], df["bb_pct"] = compute_bollinger(c)
    df["atr_14"] = compute_atr(df, 14)
    df["adx_14"], df["di_plus"], df["di_minus"] = compute_adx(df)
    df["sell_score"] = compute_sell_score(df)

    return df


def enrich_1d(df: pd.DataFrame) -> pd.DataFrame:
    """Compute daily trend context."""
    c = df["close"]
    df["ema_200"] = compute_ema(c, 200)
    df["price_above_ema200"] = (c > df["ema_200"]).astype(int)
    df["adx_14"], _, _ = compute_adx(df)
    return df


# ═══════════════════════════════════════════════════════════════
#  BOT STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

DEFAULT_STATE = {
    "coin_held": 100.0,
    "usdt_held": 0.0,
    "pending_buyback": False,
    "sell_price": 0.0,
    "coin_sold": 0.0,
    "bars_since_sell": 0,
    "last_sell_bar_hash": "",
    "current_sell_pct": 1.0,
    "total_wins": 0,
    "total_losses": 0,
    "total_gained": 0.0,
    "total_lost": 0.0,
    "trade_history": [],
    "last_signal_hash": "",
    "created_at": "",
}


def load_state(path: str) -> Dict:
    if os.path.exists(path):
        with open(path, "r") as f:
            state = json.load(f)
            # Migrate old states
            for k, v in DEFAULT_STATE.items():
                if k not in state:
                    state[k] = v
            return state
    state = DEFAULT_STATE.copy()
    state["created_at"] = datetime.now(timezone.utc).isoformat()
    return state


def save_state(state: Dict, path: str):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)
    log.info(f"State saved: {path}")


# ═══════════════════════════════════════════════════════════════
#  PUSHOVER NOTIFICATION
# ═══════════════════════════════════════════════════════════════

def send_pushover(title: str, message: str, priority: int = 0,
                  sound: str = "cashregister"):
    user_key = os.environ.get("PUSHOVER_USER_KEY", "")
    api_token = os.environ.get("PUSHOVER_API_TOKEN", "")
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    if dry_run:
        log.info(f"[DRY RUN] Pushover: {title}\n{message}")
        return True

    if not user_key or not api_token:
        log.warning("Pushover credentials not set — skipping notification")
        return False

    payload = {
        "token": api_token,
        "user": user_key,
        "title": title,
        "message": message,
        "priority": priority,
        "sound": sound,
        "html": 1,
    }

    # Priority 2 requires retry/expire
    if priority == 2:
        payload["retry"] = 60
        payload["expire"] = 300

    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=payload, timeout=10,
        )
        resp.raise_for_status()
        log.info(f"Pushover sent: {title}")
        return True
    except Exception as e:
        log.error(f"Pushover failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════

def check_sell_signal(row_4h: pd.Series, daily_ctx: Dict) -> Tuple[bool, str]:
    """Check if should SELL coin → USDT."""

    # Only in bear market (1D price < EMA200)
    if daily_ctx.get("uptrend", True):
        return False, "BULL_MARKET"

    # RSI floor
    if row_4h["rsi_14"] < CFG.sell_rsi_min:
        return False, f"RSI_LOW({row_4h['rsi_14']:.0f})"

    # Signal A: RSI peaked + momentum fading + sell pressure
    sig_a = (row_4h["rsi_14"] > CFG.sell_sig_a_rsi
             and row_4h["macd_rising"] == 0
             and row_4h.get("sell_score", 0) >= CFG.sell_sig_a_sell_score)

    # Signal B: RSI elevated + bearish structure
    sig_b = (row_4h["rsi_14"] > CFG.sell_sig_b_rsi
             and row_4h["ema_34"] < row_4h["ema_50"]
             and row_4h["macd_hist"] < 0)

    # Signal C: Strong sell score + momentum
    sig_c = (row_4h.get("sell_score", 0) >= CFG.sell_sig_c_sell_score
             and row_4h["rsi_14"] > CFG.sell_sig_c_rsi
             and row_4h.get("stoch_k", 50) > CFG.sell_sig_c_stoch)

    reasons = []
    if sig_a: reasons.append("A(RSI+MACD+SellScore)")
    if sig_b: reasons.append("B(RSI+EMA+MACD)")
    if sig_c: reasons.append("C(SellScore+RSI+Stoch)")

    if reasons:
        return True, " + ".join(reasons)
    return False, "NO_SIGNAL"


def check_buyback_signal(row_4h: pd.Series, price_change_pct: float,
                         bars_held: int) -> Tuple[bool, str]:
    """Check if should BUY BACK coin with USDT."""

    # 1. TARGET hit
    if price_change_pct <= -CFG.target_drop_pct:
        return True, "TARGET"

    # 2. Extreme oversold
    if price_change_pct < 0 and row_4h["rsi_14"] < CFG.oversold_rsi:
        return True, "OVERSOLD"

    # 3. Bounce detected while in profit
    if (price_change_pct < -0.5
            and row_4h.get("ema_34", 0) > row_4h.get("ema_50", 0)
            and row_4h["macd_rising"] == 1
            and row_4h["macd_hist"] > 0):
        return True, "BOUNCE"

    # 4. STOP — price went up too much
    if price_change_pct >= CFG.stop_rise_pct:
        return True, "STOP"

    # 5. TIMEOUT
    if bars_held >= CFG.timeout_bars:
        return True, "TIMEOUT"

    return False, ""


# ═══════════════════════════════════════════════════════════════
#  MAIN BOT LOGIC
# ═══════════════════════════════════════════════════════════════

def run_bot():
    symbol = os.environ.get("COIN_SYMBOL", "ETHUSDT")
    coin_name = os.environ.get("COIN_NAME", "ETH")
    initial_amount = float(os.environ.get("INITIAL_AMOUNT", "100"))
    state_file = os.environ.get("STATE_FILE", "state.json")

    log.info(f"═══ ETH Accumulator Bot — {symbol} ═══")
    log.info(f"Config: stop={CFG.stop_rise_pct}%, target={CFG.target_drop_pct}%, "
             f"cooldown={CFG.sell_cooldown_bars} bars, RSI≥{CFG.sell_rsi_min}")

    # ── Load state ──
    state = load_state(state_file)
    if state["coin_held"] == 0 and state["usdt_held"] == 0:
        state["coin_held"] = initial_amount
        log.info(f"Initialized with {initial_amount} {coin_name}")

    # ── Fetch market data ──
    log.info(f"Fetching {symbol} 4H klines...")
    df_4h = fetch_klines(symbol, "4h", limit=300)
    log.info(f"Fetching {symbol} 1D klines...")
    df_1d = fetch_klines(symbol, "1d", limit=250)

    # ── Compute indicators ──
    df_4h = enrich_4h(df_4h)
    df_1d = enrich_1d(df_1d)

    # ── Determine closed vs live bar ──
    # Nến cuối cùng từ Binance có thể chưa đóng (đang chạy).
    # SELL signals: chỉ dùng nến ĐÃ ĐÓNG (iloc[-2]) → indicators ổn định
    # BUYBACK signals: dùng giá REALTIME (iloc[-1]) → phản ứng nhanh khi hit target/stop
    now_utc = datetime.now(timezone.utc)
    last_bar_open = df_4h.iloc[-1]["datetime"]
    last_bar_close_time = last_bar_open + pd.Timedelta(hours=4)
    live_bar_is_closed = now_utc >= last_bar_close_time.to_pydatetime().replace(
        tzinfo=timezone.utc) if hasattr(last_bar_close_time, 'to_pydatetime') else True

    # Nến đã đóng hoàn toàn → dùng làm signal nến
    # Nến chưa đóng → dùng nến trước đó cho SELL, nến hiện tại cho giá realtime
    if live_bar_is_closed:
        signal_bar = df_4h.iloc[-1]   # Nến vừa đóng
        log.info(f"Bar CLOSED: {last_bar_open}")
    else:
        signal_bar = df_4h.iloc[-2]   # Nến đóng gần nhất
        log.info(f"Bar LIVE (chưa đóng): {last_bar_open}, dùng signal từ {df_4h.iloc[-2]['datetime']}")

    # Giá realtime luôn lấy từ nến cuối
    realtime_price = df_4h.iloc[-1]["close"]
    signal_price = signal_bar["close"]
    bar_time = signal_bar["datetime"]

    # Match 1D context
    today_str = str(bar_time)[:10]
    daily_match = df_1d[df_1d["datetime"].dt.strftime("%Y-%m-%d") <= today_str]
    if len(daily_match) == 0:
        log.error("No daily data matched")
        return

    daily_row = daily_match.iloc[-1]
    daily_ctx = {
        "uptrend": bool(daily_row.get("price_above_ema200", 0)),
        "adx": float(daily_row.get("adx_14", 0)),
    }

    # ── Dedup: skip nếu cùng signal bar đã xử lý (cho SELL) ──
    bar_hash = hashlib.md5(f"{bar_time}_{signal_price}".encode()).hexdigest()[:12]
    sell_already_processed = (bar_hash == state.get("last_signal_hash", ""))

    # ── Log current state ──
    price = realtime_price  # Dùng giá realtime để hiển thị & tính buyback
    total_coin = state["coin_held"] + state["usdt_held"] / price if price > 0 else state["coin_held"]
    market_phase = "🐻 BEAR" if not daily_ctx["uptrend"] else "🐂 BULL"
    log.info(f"Signal bar: {bar_time} | Realtime: ${price:,.2f} | {market_phase}")
    log.info(f"Holdings: {state['coin_held']:.4f} {coin_name} + ${state['usdt_held']:,.2f} USDT "
             f"= {total_coin:.4f} {coin_name} equiv")
    log.info(f"Sell%: {state['current_sell_pct']*100:.0f}% | "
             f"W:{state['total_wins']} L:{state['total_losses']}")

    # ── DECISION LOGIC ──
    action = None
    signal_msg = ""

    if state["pending_buyback"]:
        # ── CHECK BUYBACK (dùng giá REALTIME — không cần đợi nến đóng) ──
        pchg = (price / state["sell_price"] - 1) * 100

        # Chỉ tăng bars_since_sell khi signal bar mới (tránh đếm trùng)
        if not sell_already_processed:
            state["bars_since_sell"] += 1

        log.info(f"Pending buyback: sold@${state['sell_price']:,.2f}, "
                 f"now ${price:,.2f} ({pchg:+.2f}%), "
                 f"bars={state['bars_since_sell']}")

        # Dùng signal_bar cho indicator checks (BOUNCE, OVERSOLD)
        # nhưng pchg dùng realtime price (TARGET, STOP phản ứng nhanh)
        should_buy, reason = check_buyback_signal(
            signal_bar, pchg, state["bars_since_sell"])

        if should_buy:
            action = "BUYBACK"
            comm = CFG.commission_pct / 100
            coin_bought = state["usdt_held"] / price * (1 - comm)
            coin_gained = coin_bought - state["coin_sold"]
            is_loss = reason in ("STOP", "TIMEOUT") or coin_gained < 0

            # Update state
            state["coin_held"] += coin_bought
            state["usdt_held"] = 0.0
            state["pending_buyback"] = False

            if is_loss:
                state["current_sell_pct"] = max(
                    CFG.min_sell_pct,
                    state["current_sell_pct"] - CFG.reduce_on_loss)
                state["total_losses"] += 1
                state["total_lost"] += coin_gained
            else:
                state["current_sell_pct"] = min(
                    CFG.initial_sell_pct,
                    state["current_sell_pct"] + CFG.increase_on_win)
                state["total_wins"] += 1
                state["total_gained"] += coin_gained

            # Trade record
            trade_record = {
                "time": str(bar_time),
                "action": "BUYBACK",
                "reason": reason,
                "price": round(price, 2),
                "sell_price": round(state["sell_price"], 2),
                "price_change_pct": round(pchg, 2),
                "coin_bought": round(coin_bought, 6),
                "coin_gained": round(coin_gained, 6),
                "coin_held": round(state["coin_held"], 6),
                "bars_held": state["bars_since_sell"],
                "next_sell_pct": round(state["current_sell_pct"], 2),
            }
            state["trade_history"].append(trade_record)

            emoji = "✅" if not is_loss else "🔴"
            priority = 0 if not is_loss else 1

            signal_msg = (
                f"{emoji} <b>BUY BACK {coin_name}</b>\n\n"
                f"Lý do: <b>{reason}</b>\n"
                f"Giá sell: ${state['sell_price']:,.2f}\n"
                f"Giá buy: <b>${price:,.2f}</b> ({pchg:+.1f}%)\n"
                f"Hold: {state['bars_since_sell']} bars ({state['bars_since_sell']*4}h)\n\n"
                f"{'📦 MUA LẠI':}\n"
                f"• Dùng: <b>${state['usdt_held'] + coin_bought*price:,.2f} USDT</b>\n"
                f"• Mua: <b>{coin_bought:.4f} {coin_name}</b>\n"
                f"• ETH thay đổi: <b>{coin_gained:+.4f}</b>\n\n"
                f"📊 Sau trade:\n"
                f"• Nắm giữ: <b>{state['coin_held']:.4f} {coin_name}</b>\n"
                f"• Tổng +/-: {state['total_gained']+state['total_lost']:+.4f} {coin_name}\n"
                f"• W/L: {state['total_wins']}/{state['total_losses']}\n"
                f"• Sell% tiếp: {state['current_sell_pct']*100:.0f}%"
            )

            send_pushover(
                f"{emoji} BUY {coin_name} — {reason} ({pchg:+.1f}%)",
                signal_msg,
                priority=priority,
                sound="cashregister" if not is_loss else "falling",
            )

    else:
        # ── CHECK SELL ──
        # Cooldown check via bar count
        bars_since_last = state.get("bars_since_sell", 999)
        cooldown_ok = bars_since_last >= CFG.sell_cooldown_bars

        if not cooldown_ok:
            log.info(f"Cooldown: {bars_since_last}/{CFG.sell_cooldown_bars} bars")
            if not sell_already_processed:
                state["bars_since_sell"] = bars_since_last + 1
        elif sell_already_processed:
            # Signal bar đã xử lý rồi → chờ nến mới đóng
            log.info(f"Signal bar {bar_time} already processed — waiting for next close")
        else:
            # SELL dùng signal_bar (nến ĐÃ ĐÓNG) cho indicators
            should_sell, sell_reason = check_sell_signal(signal_bar, daily_ctx)

            if should_sell:
                action = "SELL"
                sell_pct = state["current_sell_pct"]
                sell_amount = state["coin_held"] * sell_pct
                comm = CFG.commission_pct / 100
                # Sell ở giá realtime (không phải giá close nến cũ)
                usdt_received = sell_amount * price * (1 - comm)

                state["coin_held"] -= sell_amount
                state["usdt_held"] += usdt_received
                state["pending_buyback"] = True
                state["sell_price"] = price
                state["coin_sold"] = sell_amount
                state["bars_since_sell"] = 0

                trade_record = {
                    "time": str(datetime.now(timezone.utc)),
                    "signal_bar": str(bar_time),
                    "action": "SELL",
                    "reason": sell_reason,
                    "price": round(price, 2),
                    "coin_sold": round(sell_amount, 6),
                    "usdt_received": round(usdt_received, 2),
                    "coin_held": round(state["coin_held"], 6),
                    "sell_pct": round(sell_pct, 2),
                }
                state["trade_history"].append(trade_record)

                # Compute targets
                target_price = price * (1 - CFG.target_drop_pct / 100)
                stop_price = price * (1 + CFG.stop_rise_pct / 100)

                signal_msg = (
                    f"🔔 <b>SELL {coin_name} → USDT</b>\n\n"
                    f"Signals: {sell_reason}\n"
                    f"Giá: <b>${price:,.2f}</b>\n"
                    f"Sell: <b>{sell_pct*100:.0f}%</b> = "
                    f"<b>{sell_amount:.4f} {coin_name}</b>\n"
                    f"Nhận: <b>${usdt_received:,.2f} USDT</b>\n\n"
                    f"🎯 Mục tiêu mua lại:\n"
                    f"• Target: ${target_price:,.2f} (-{CFG.target_drop_pct}%)\n"
                    f"• Stop:   ${stop_price:,.2f} (+{CFG.stop_rise_pct}%)\n"
                    f"• Timeout: {CFG.timeout_bars} bars ({CFG.timeout_bars*4}h)\n\n"
                    f"📊 Còn lại: {state['coin_held']:.4f} {coin_name}\n"
                    f"RSI={signal_bar['rsi_14']:.1f} MACD_hist={signal_bar['macd_hist']:.2f}"
                )

                send_pushover(
                    f"🔔 SELL {sell_pct*100:.0f}% {coin_name} @ ${price:,.0f}",
                    signal_msg,
                    priority=1,
                    sound="pushover",
                )

            else:
                log.info(f"No sell signal — {sell_reason}")

    # ── Status report (every check) ──
    if action is None and not state["pending_buyback"]:
        # Quiet — no action, just log
        log.info("No action this bar")

    elif action is None and state["pending_buyback"]:
        pchg = (price / state["sell_price"] - 1) * 100
        target_price = state["sell_price"] * (1 - CFG.target_drop_pct / 100)
        stop_price = state["sell_price"] * (1 + CFG.stop_rise_pct / 100)
        log.info(f"Waiting for buyback: price {pchg:+.1f}% from sell "
                 f"(target ${target_price:,.0f} / stop ${stop_price:,.0f})")

    # ── Save state ──
    state["last_signal_hash"] = bar_hash
    # Trim history to last 100 trades
    if len(state["trade_history"]) > 100:
        state["trade_history"] = state["trade_history"][-100:]

    save_state(state, state_file)
    log.info("═══ Done ═══\n")


# ═══════════════════════════════════════════════════════════════
#  HEALTH CHECK (for Koyeb)
# ═══════════════════════════════════════════════════════════════

def run_healthcheck():
    """Simple HTTP health endpoint for Koyeb."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state_file = os.environ.get("STATE_FILE", "state.json")
            state = load_state(state_file) if os.path.exists(state_file) else {}
            coin = state.get("coin_held", 0)
            usdt = state.get("usdt_held", 0)
            pending = state.get("pending_buyback", False)
            w = state.get("total_wins", 0)
            l = state.get("total_losses", 0)

            body = json.dumps({
                "status": "ok",
                "coin_held": coin,
                "usdt_held": usdt,
                "pending_buyback": pending,
                "wins": w,
                "losses": l,
                "updated": state.get("updated_at", ""),
            }, indent=2)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass  # Suppress access logs

    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Health check server on :{port}")


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    mode = os.environ.get("BOT_MODE", "once")

    if mode == "server":
        # Server mode: health check + periodic runs
        run_healthcheck()
        interval = int(os.environ.get("CHECK_INTERVAL", "14400"))  # 4h default
        log.info(f"Server mode: checking every {interval}s")

        while True:
            try:
                run_bot()
            except Exception as e:
                log.exception(f"Bot error: {e}")
                try:
                    send_pushover("❌ Bot Error", str(e)[:500], priority=1, sound="siren")
                except:
                    pass
            time.sleep(interval)

    else:
        # One-shot mode (for cron / Koyeb scheduled jobs)
        try:
            run_bot()
        except Exception as e:
            log.exception(f"Bot error: {e}")
            try:
                send_pushover("❌ Bot Error", str(e)[:500], priority=1, sound="siren")
            except:
                pass
            sys.exit(1)


if __name__ == "__main__":
    main()
