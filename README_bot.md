# ETH Accumulator Signal Bot

Bot báo tín hiệu SELL/BUY ETH trong bear market qua Pushover.

## Quick Start (Koyeb)

### 1. Push code lên GitHub

```bash
git init
git add eth_accumulator_bot.py requirements.txt Dockerfile
git commit -m "ETH accumulator bot"
git remote add origin https://github.com/YOUR/repo.git
git push -u origin main
```

### 2. Deploy Koyeb

- Tạo service mới từ GitHub repo
- Build: **Dockerfile**
- Instance: **Nano** ($2.7/mo) — bot rất nhẹ
- Port: **8000**
- Health check: `GET /` trên port 8000

### 3. Environment Variables

| Variable | Required | Default | Mô tả |
|---|---|---|---|
| `PUSHOVER_USER_KEY` | ✅ | — | Pushover user key |
| `PUSHOVER_API_TOKEN` | ✅ | — | Pushover app API token |
| `COIN_SYMBOL` | — | `ETHUSDT` | Trading pair trên Binance |
| `COIN_NAME` | — | `ETH` | Tên coin hiển thị |
| `INITIAL_AMOUNT` | — | `100` | Số coin bắt đầu |
| `STATE_FILE` | — | `state.json` | File lưu trạng thái |
| `BOT_MODE` | — | `server` | `server` (liên tục) hoặc `once` (1 lần) |
| `CHECK_INTERVAL` | — | `14400` | Giây giữa mỗi lần check (4h=14400) |
| `DRY_RUN` | — | `false` | `true` để test không gửi notification |

### 4. Lấy Pushover keys

1. Tạo tài khoản tại https://pushover.net
2. **User Key**: hiện trên dashboard sau khi login
3. **API Token**: Create Application → lấy API Token

## Cách hoạt động

```
Mỗi 4 giờ:
  ├─ Fetch dữ liệu Binance (4H + 1D klines)
  ├─ Tính indicators (RSI, MACD, EMA, Stoch, BB, ADX...)
  ├─ Check trạng thái hiện tại
  │   ├─ Nếu ĐANG GIỮ ETH → check sell signal
  │   │   ├─ Bear market + RSI≥58 + breakdown signals → 🔔 SELL
  │   │   └─ Không đủ điều kiện → chờ
  │   └─ Nếu ĐÃ SELL (chờ buyback) → check buyback
  │       ├─ Giá giảm ≥2% → ✅ BUY TARGET
  │       ├─ Bounce detected + giá thấp hơn → ✅ BUY BOUNCE
  │       ├─ RSI<25 extreme → ✅ BUY OVERSOLD
  │       ├─ Giá tăng ≥1.5% → 🔴 BUY STOP (cắt lỗ)
  │       └─ Quá 36 bars → BUY TIMEOUT
  └─ Gửi Pushover notification nếu có action
```

## Adaptive Sell%

Bot tự điều chỉnh lượng ETH sell mỗi lần:

- Bắt đầu: **100%** (sell all)
- Sau mỗi lần STOP (thua): giảm **30%** (100→70→40→20%)
- Sau mỗi lần TARGET (thắng): tăng **10%** (20→30→40→...)
- Sàn: **20%** (không bao giờ sell ít hơn 20%)

→ Tự động giảm risk khi market choppy, tăng lại khi có edge.

## Health Check

```bash
curl http://localhost:8000/
# {"status": "ok", "coin_held": 100.0, "pending_buyback": false, ...}
```

## Test local

```bash
# Dry run (không gửi notification)
DRY_RUN=true python eth_accumulator_bot.py

# Với Pushover
PUSHOVER_USER_KEY=xxx PUSHOVER_API_TOKEN=yyy python eth_accumulator_bot.py
```

## Notifications

### 🔔 SELL Signal
```
🔔 SELL 100% ETH @ $2,050
Signals: A(RSI+MACD+SellScore)
Giá: $2,050.00
Sell: 100% = 100.0000 ETH
Nhận: $204,846.25 USDT

🎯 Mục tiêu mua lại:
• Target: $2,009.00 (-2%)
• Stop:   $2,080.75 (+1.5%)
• Timeout: 36 bars (144h)
```

### ✅ BUY Signal (thắng)
```
✅ BUY ETH — TARGET (-2.3%)
Giá sell: $2,050.00
Giá buy: $2,002.85 (-2.3%)
• Mua: 102.2087 ETH
• ETH thay đổi: +2.2087

Sau trade:
• Nắm giữ: 102.2087 ETH
• W/L: 1/0
```

### 🔴 BUY Signal (thua)
```
🔴 BUY ETH — STOP (+1.6%)
Giá sell: $2,050.00
Giá buy: $2,082.80 (+1.6%)
• Mua: 98.2985 ETH
• ETH thay đổi: -1.7015

Sell% tiếp: 70%
```

## Backtest Results (2025-11 → 2026-03)

| Metric | Kết quả |
|---|---|
| ETH tăng thêm | **+16.95** |
| MaxDD | -5.1% |
| Win Rate | 60% |
| Wins / Losses | 12 / 8 |
